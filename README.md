# Swiss labour tracker

Monthly registered unemployment by canton, plus Aussteuerungen (people whose
ALV entitlement ran out), pulled from SECO and published as a static dashboard.

    fetch_seco.py  ->  data/labour.json  ->  docs/index.html  (GitHub Pages)

A GitHub Action re-runs the fetcher on the 9th of each month and commits the
refreshed JSON. No server.

## Setup

1. Push this repo to GitHub.
2. Settings -> Pages -> deploy from branch `main`, folder `/docs`.
3. Settings -> Actions -> General -> allow "Read and write permissions".
4. Actions tab -> "Update labour data" -> Run workflow.

## Calibrate the parser first

The bulletin is a PDF, so the extraction rules need one pass against a real
file before you trust the numbers:

    pip install -r requirements.txt
    python fetch_seco.py --debug 2026-04

That prints the raw text of the pages it latched onto. Compare it against the
regexes in `parse_cantons` and `parse_national` and adjust. Then:

    python fetch_seco.py --months 36
    cp data/labour.json docs/labour.json

## Known limits

- **History only goes back to January 2025.** SECO retired the old
  per-year DAM folders in 2026 in favour of opaque, unguessable download
  links, so the fetcher now scrapes the current bulletin archive page
  (`arbeitsmarktstatistik-berichte-rechtsgrundlagen`) for direct PDF links
  instead of constructing URLs from the year/month. That page only lists
  the last ~19 months; `--months` beyond that just logs "no bulletin found"
  for the older ones rather than failing. If SECO's archive page ever grows
  a real paginated history, `fetch_bulletin_index()` in `fetch_seco.py` is
  the place to extend it.
- **Aussteuerungen are national only.** The SECO bulletin publishes the canton
  breakdown for unemployment but not for benefit exhaustion, so the
  "share of unemployed" figure is a national ratio. Canton-level Ausgesteuerte
  exist at individual cantonal statistical offices (St. Gallen and Zürich both
  publish them) and would need one adapter each.
- **Timing mismatch.** Unemployment is a month-end stock; Aussteuerungen are a
  flow over the month. The ratio is a useful indicator, not a clean percentage
  of a population.
- **PDF parsing is brittle.** If SECO restyles the bulletin the regexes will
  need a nudge. The Action will commit an unchanged file rather than bad data,
  and `--debug` is how you find out what moved.
