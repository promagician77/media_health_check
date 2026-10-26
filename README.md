# Media Data Health Check

Prepared for the Part and Sum Business Analyst (Media Data and MarTech) application.

A synthetic four-client media warehouse (Meta Ads, Google Ads, DV360, GA4) with **eleven faults hidden in it**,
eleven SQL checks that have to find them, and a tabbed report of what they found.
**Nothing here is a client's data.** Every spend, click, session and event number is generated.

## What it shows

| Tab | The point |
|---|---|
| Board | 11 checks x 4 clients. Every alert is written in plain language: what was seen, why it matters, who fixes it. |
| GA4 export gap | Standard GA4 properties have a 1M events/day BigQuery export limit. Above it the export pauses and missed days are not reprocessed. An early-warning check fires 7 days before the first missing day. |
| Day shift | A Pacific-time account loaded by UTC date: 25 of 28 days wrong, period total 0.27% off. |
| Channels | A paid Meta campaign tagged `utm_medium=social` is filed as Organic Social by GA4's channel rules. |
| Conversions | Meta returns a purchase under overlapping action types; a dashboard field that adds them reads 2.9x. A dashboard field pinned to the `7d_view` window Meta removed on 12 Jan 2026. |
| Request queue | A transparent score that ranks reporting requests and sets the reply time. |
| SQL and scope | All SQL (DuckDB executed; BigQuery and Snowflake machine-translated), and exactly what is measured vs modelled. |

## Measured

* The checks are SQL. Their output is computed, not typed.
* Seed 42: **11 of 11** seeded faults found, 0 unexplained alerts, 6 follow-on symptoms the answer key allows.
* **20 clean seeds x 112 checks = 2,240 checks, 0 alerts.**
* **20 other faulted seeds: 220 of 220 faults found**, 0 unexplained alerts.
* 24 of 24 GA4 channel-rule cases match Google's published rules (tests the SQL against the written rules, not against GA4's own engine).
* Thresholds (1%, 70%, 50%, 85%) were fixed before validation and never changed. The first clean run raised one alert on all 20 seeds; the cause was a generator bug (a fault ramp not switched off in clean data), so the generator was fixed.

## Modelled (invented, and said so)

* All volumes, CPCs, conversion rates and the hourly spend curve are made up.
* The fault shapes are plausible, not observed. Google does not publish the exact GA4 export pause trigger; the model pauses after four consecutive days over 1M.
* The channel rule uses an abbreviated list of social and search sites.
* BigQuery and Snowflake SQL is machine-translated with sqlglot and has **not** been run on those warehouses.
* Thresholds need roughly two weeks of calibration per client on real data.
* Request-queue weights are a proposal.

## Run it

```bash
pip install duckdb numpy pandas sqlglot
python3 gen_data.py --seed 42          # build the warehouse (media.duckdb)
python3 run_checks.py --validate       # run checks, score, 40-seed validation, write site/data/results.js
python3 run_checks.py --seed 7 --clean # a clean warehouse should print 0 alerts
```

## Deploy (Vercel)

Static site, no build step. `vercel.json` sets `outputDirectory` to `site` and marks every page `noindex`.

```bash
npx vercel --prod
```

## Platform facts used

* GA4 BigQuery export limit and late-arriving data: https://support.google.com/analytics/answer/9358801
* What happens above the limit: https://support.google.com/analytics/answer/9823238
* GA4 default channel group rules: https://support.google.com/analytics/answer/9756891
* Meta action types overlap: https://help.funnel.io/en/articles/17680416-facebook-pivoted-action-types-working-with-the-data
* Meta removes 7d_view / 28d_view (12 Jan 2026): https://ppc.land/meta-restricts-attribution-windows-and-data-retention-in-ads-insights-api/
