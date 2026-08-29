#!/usr/bin/env python3
"""
Pulls SECO's monthly 'Die Lage auf dem Arbeitsmarkt' bulletin and turns it into
tidy JSON for the dashboard.

Two series come out of each bulletin:
  - unemployment by canton (Arbeitslose + Arbeitslosenquote), monthly
  - Aussteuerungen from the ALV, monthly, NATIONAL only

Run a calibration pass first:
    python fetch_seco.py --debug 2026-04
That prints the raw text of the pages the parser latched onto, so you can see
exactly what it is reading before trusting the numbers.

Normal run:
    python fetch_seco.py --months 36
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber missing. Run: pip install -r requirements.txt")

OUT = Path(__file__).parent / "data" / "labour.json"
CACHE = Path(__file__).parent / ".cache"

# SECO's old per-year DAM folders (lage_arbeitsmarkt_{year}/...) were retired in
# 2026 in favour of opaque /dam/de/sd-web/{hash}/... links that can't be guessed
# from the year/month. Instead we scrape this archive page, which lists every
# recent bulletin with its real download link, keyed by the YYYY-MM in the
# filename itself.
ARCHIVE_URL = "https://www.seco.admin.ch/de/arbeitsmarktstatistik-berichte-rechtsgrundlagen"
ARCHIVE_LINK = re.compile(
    r'href="(https://www\.seco\.admin\.ch/dam/de/sd-web/[^"]+/'
    r"(\d{4}-\d{2})_Die_Lage_auf_dem_Arbeitsmarkt_DE\.pdf)\""
)

CANTONS = {
    "Zürich": "ZH", "Bern": "BE", "Luzern": "LU", "Uri": "UR", "Schwyz": "SZ",
    "Obwalden": "OW", "Nidwalden": "NW", "Glarus": "GL", "Zug": "ZG",
    "Freiburg": "FR", "Solothurn": "SO", "Basel-Stadt": "BS",
    "Basel-Landschaft": "BL", "Schaffhausen": "SH", "Appenzell A.-Rh.": "AR",
    "Appenzell A. Rh.": "AR", "Appenzell Ausserrhoden": "AR",
    "AppenzellAusserrhoden": "AR",
    "Appenzell I.-Rh.": "AI", "Appenzell I. Rh.": "AI",
    "Appenzell Innerrhoden": "AI", "AppenzellInnerrhoden": "AI",
    "St. Gallen": "SG", "St.Gallen": "SG",
    "Graubünden": "GR", "Aargau": "AG", "Thurgau": "TG", "Tessin": "TI",
    "Waadt": "VD", "Wallis": "VS", "Neuenburg": "NE", "Genf": "GE",
    "Jura": "JU",
}

# The display name for each code — independent of which raw-text variant
# above (glued, hyphenated, ...) actually matched in a given bulletin.
CANTON_DISPLAY = {
    "ZH": "Zürich", "BE": "Bern", "LU": "Luzern", "UR": "Uri", "SZ": "Schwyz",
    "OW": "Obwalden", "NW": "Nidwalden", "GL": "Glarus", "ZG": "Zug",
    "FR": "Freiburg", "SO": "Solothurn", "BS": "Basel-Stadt",
    "BL": "Basel-Landschaft", "SH": "Schaffhausen",
    "AR": "Appenzell Ausserrhoden", "AI": "Appenzell Innerrhoden",
    "SG": "St. Gallen", "GR": "Graubünden", "AG": "Aargau", "TG": "Thurgau",
    "TI": "Tessin", "VD": "Waadt", "VS": "Wallis", "NE": "Neuenburg",
    "GE": "Genf", "JU": "Jura",
}

# Longest names first so "Basel-Landschaft" wins over "Basel-Stadt" prefixes etc.
CANTON_PATTERN = re.compile(
    "^(" + "|".join(re.escape(n) for n in sorted(CANTONS, key=len, reverse=True)) + r")\b(.*)$"
)

NUM = re.compile(r"-?[\d'’\u2019]+(?:[.,]\d+)?")


def to_num(raw: str) -> float | None:
    cleaned = raw.replace("'", "").replace("’", "").replace("\u2019", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def months_back(n: int) -> list[str]:
    today = date.today()
    out, y, m = [], today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y}-{m:02d}")
    return out


HEADERS = {"User-Agent": "swiss-labour-tracker/1.0"}


def fetch_bulletin_index() -> dict[str, str]:
    """Scrape the archive page once per run: {"2026-07": "https://.../2026-07_..._DE.pdf"}."""
    try:
        r = requests.get(ARCHIVE_URL, timeout=60, headers=HEADERS)
    except requests.RequestException as e:
        print(f"  could not reach archive page: {e}", file=sys.stderr)
        return {}
    if r.status_code != 200:
        print(f"  archive page returned {r.status_code}", file=sys.stderr)
        return {}
    return {ym: url for url, ym in ARCHIVE_LINK.findall(r.text)}


def download(ym: str, index: dict[str, str]) -> bytes | None:
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"{ym}.pdf"
    if cached.exists():
        return cached.read_bytes()

    url = index.get(ym)
    if not url:
        return None
    try:
        r = requests.get(url, timeout=60, headers=HEADERS)
    except requests.RequestException:
        return None
    if r.status_code == 200 and r.content[:4] == b"%PDF":
        cached.write_bytes(r.content)
        return r.content
    return None


def parse_cantons(pdf, debug: bool = False) -> dict[str, dict]:
    """Read the two 'nach Kantonen' tables on one page: Arbeitslose (counts)
    then Arbeitslosenquote (rates). Each canton row's first number is the
    current-month reading for whichever table we're in — a magnitude
    heuristic isn't reliable here since the count table's own %-change
    columns (e.g. "+1,1") are small enough to be mistaken for a rate."""
    rows: dict[str, dict] = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        # pdfplumber drops the inter-word gap on some headers in this layout
        # ("ArbeitslosenachKantonen"), so match with the space optional.
        if not re.search(r"nach\s*Kanton", text):
            continue
        if debug:
            print(f"\n--- page {page.page_number} ---\n{text}\n")
        section = None
        for line in text.split("\n"):
            line = line.strip()
            if "Tabelle" in line and "Kanton" in line:
                section = "rate" if "quote" in line.lower() else "count"
                continue
            if section is None:
                continue
            m = CANTON_PATTERN.match(line)
            if not m:
                continue
            name, rest = m.group(1), m.group(2)
            nums = [to_num(x) for x in NUM.findall(rest)]
            nums = [n for n in nums if n is not None]
            if not nums:
                continue
            code = CANTONS[name]
            entry = rows.setdefault(code, {"code": code, "name": CANTON_DISPLAY[code]})
            if section == "count" and "unemployed" not in entry:
                entry["unemployed"] = int(nums[0])
            elif section == "rate" and "rate" not in entry:
                entry["rate"] = nums[0]
        if rows:
            break
    return rows


def parse_national(pdf, debug: bool = False) -> dict:
    """National totals plus the monthly Aussteuerungen figure."""
    out: dict = {}
    for page in pdf.pages:
        text = page.extract_text() or ""
        # Undo mid-word line-wrap hyphenation ("ver-\nharrte" -> "verharrte"),
        # which otherwise hides the verb from the regexes below and makes the
        # search skip ahead to the wrong (seasonally-adjusted) sentence.
        text = re.sub(r"-\n", "", text)

        # pdfplumber drops the inter-word gap on some pages entirely (a whole
        # sentence with zero spaces) and only partway through on others, so
        # every gap here is optional (\s*), never required (\s+).
        if "unemployed" not in out:
            m = re.search(r"Arbeitslosen\s*(?:erh[öo]hte|verringerte|sank|stieg|verharrte)"
                          r".{0,160}?auf\s*([\d'’\u2019]+)", text, re.S)
            if m:
                v = to_num(m.group(1))
                if v and v > 10000:
                    out["unemployed"] = int(v)

        if "rate" not in out:
            m = re.search(r"Arbeitslosenquote\s*(?:stieg|sank|verharrte|erh[öo]hte)"
                          r".{0,160}?(?:auf|bei)\s*([\d]+[.,]\d)\s*%", text, re.S)
            if m:
                out["rate"] = to_num(m.group(1))

        if "aussteuerungen" not in out and "ussteuerung" in text:
            # Match only the actual data-table row ("Aussteuerungen 2’629 ..."),
            # not the table of contents or chart captions — the old wide,
            # unanchored lookahead picked up "...seit 2004" as the figure.
            m = re.search(r"Aussteuerungen\s+([\d'’\u2019]{3,})", text)
            if debug and m:
                print(f"\n--- Aussteuerungen page {page.page_number} ---\n{text}\n")
            if m:
                v = to_num(m.group(1))
                if v and 100 < v < 100000:
                    out["aussteuerungen"] = int(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=36, help="how many months back to pull")
    ap.add_argument("--debug", metavar="YYYY-MM", help="dump raw page text for one month")
    args = ap.parse_args()

    targets = [args.debug] if args.debug else months_back(args.months)
    national: list[dict] = []
    cantons: dict[str, dict] = {}
    index = fetch_bulletin_index()

    for ym in targets:
        blob = download(ym, index)
        if not blob:
            print(f"  {ym}  no bulletin found", file=sys.stderr)
            continue
        with pdfplumber.open(io.BytesIO(blob)) as pdf:
            nat = parse_national(pdf, debug=bool(args.debug))
            can = parse_cantons(pdf, debug=bool(args.debug))

        if nat:
            national.append({"period": ym, **nat})
        for code, row in can.items():
            slot = cantons.setdefault(code, {"code": code, "name": row["name"], "months": []})
            slot["months"].append({
                "period": ym,
                "unemployed": row.get("unemployed"),
                "rate": row.get("rate"),
            })
        print(f"  {ym}  national={bool(nat)}  cantons={len(can)}", file=sys.stderr)

    if args.debug:
        return

    national.sort(key=lambda r: r["period"])
    for c in cantons.values():
        c["months"].sort(key=lambda r: r["period"])

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": date.today().isoformat(),
        "source": "SECO, Die Lage auf dem Arbeitsmarkt (amstat.ch)",
        "notes": "Aussteuerungen are national only in this bulletin.",
        "national": national,
        "cantons": sorted(cantons.values(), key=lambda c: c["code"]),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT} — {len(national)} months, {len(cantons)} cantons")


if __name__ == "__main__":
    main()
