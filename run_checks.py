#!/usr/bin/env python3
"""
Run the SQL checks against a warehouse, score them against the fault key, and write the
data the report site reads.

    python3 run_checks.py                  # seed 42, faulted, writes site/data/results.js
    python3 run_checks.py --validate       # + 20 clean seeds and 20 other faulted seeds
    python3 run_checks.py --seed 7 --clean # one clean run, prints alerts only
"""
import argparse
import datetime as dt
import json
import pathlib
import sys

import duckdb
import numpy as np
import logging

import sqlglot

logging.getLogger('sqlglot').setLevel(logging.ERROR)

import gen_data as G

ROOT = pathlib.Path(__file__).parent
SQL = ROOT / "sql"

CHECKS = [  # id, file, label, one-line purpose, severity, owner
    ("freshness",       "01_freshness.sql",       "Freshness",            "Is yesterday loaded?",                                   "high", "Data engineering"),
    ("completeness",    "02_completeness.sql",    "Completeness",         "Missing days and partial loads",                         "high", "Data engineering"),
    ("duplicates",      "03_duplicates.sql",      "Duplicates",           "More than one row per day and campaign",                 "high", "Data engineering"),
    ("reconcile_total", "04_reconcile_total.sql", "Spend reconciliation", "Warehouse vs the platform's own daily total (1%)",      "high", "Data engineering"),
    ("day_boundary",    "05_day_boundary.sql",    "Day boundary",         "Totals match but days don't: a time-zone shift",         "med",  "Data engineering"),
    ("naming",          "06_naming.sql",          "Naming convention",    "Campaign names follow the agreed pattern",               "low",  "Trafficking"),
    ("utm_channel",     "07_utm_channel.sql",     "UTM to channel",       "Paid campaigns land in their paid GA4 channel",          "high", "Trafficking + MarTech"),
    ("utm_values",      "08_utm_values.sql",      "UTM values",           "One spelling per source/medium, on the allow-list",      "med",  "Trafficking + MarTech"),
    ("tag_ratio",       "09_tag_ratio.sql",       "Tag health",           "GA4 sessions per paid click hasn't collapsed",           "high", "MarTech"),
    ("definition",      "10_definition.sql",      "Metric definition",    "Registry fields don't double count or use dead windows", "high", "Measurement"),
    ("ga4_headroom",    "11_ga4_headroom.sql",    "GA4 export headroom",  "Early warning before the 1M events/day export limit",    "med",  "MarTech"),
]
CHECK_IDS = [c[0] for c in CHECKS]
SOURCES = ("meta", "gads", "dv360", "ga4")
APPLICABLE = {
    "freshness": [(c, s) for c in G.CLIENTS for s in SOURCES],
    "completeness": [(c, s) for c in G.CLIENTS for s in SOURCES],
    "duplicates": [(c, s) for c in G.CLIENTS for s in SOURCES],
    "reconcile_total": [(c, s) for c in G.CLIENTS for s in SOURCES[:3]],
    "day_boundary": [(c, s) for c in G.CLIENTS for s in SOURCES[:3]],
    "naming": [(c, s) for c in G.CLIENTS for s in SOURCES[:3]],
    "utm_channel": [(c, "ga4") for c in G.CLIENTS],
    "utm_values": [(c, "ga4") for c in G.CLIENTS],
    "tag_ratio": [(c, s) for c in G.CLIENTS for s in SOURCES[:3]],
    "definition": [(c, "meta") for c in G.CLIENTS],
    "ga4_headroom": [(c, "ga4") for c in G.CLIENTS],
}
N_CELLS = sum(len(v) for v in APPLICABLE.values())  # 112

SRC_LABEL = {"meta": "Meta Ads", "gads": "Google Ads", "dv360": "DV360", "ga4": "GA4"}


# ------------------------------------------------------------------------------ plumbing
def connect_memory(frames):
    con = duckdb.connect(":memory:")
    for name, frame in frames.items():
        con.register("_f", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _f")
        con.unregister("_f")
    con.execute((SQL / "00_staging.sql").read_text())
    return con


def run_all(con):
    out = {}
    for cid, fname, *_ in CHECKS:
        out[cid] = con.execute((SQL / fname).read_text()).df()
    return out


def tuples(results):
    t = set()
    for cid, df in results.items():
        for _, r in df.iterrows():
            t.add((r["client_id"], r["source"], cid))
    return t


def score(alert_tuples, faults=True):
    primary = {(f["client"], f["source"], f["primary"]): f for f in G.FAULTS} if faults else {}
    allowed = set()
    if faults:
        for f in G.FAULTS:
            for c in f["also_ok"]:
                allowed.add((f["client"], f["source"], c))
            # the follow-on reconcile alert shows up under the platform's own source
        for e in G.EARLY_WARNING:
            allowed.add(e)
    caught = [k for k in primary if k in alert_tuples]
    missed = [k for k in primary if k not in alert_tuples]
    followon = [a for a in alert_tuples if a in allowed and a not in primary]
    unexplained = [a for a in alert_tuples if a not in primary and a not in allowed]
    return dict(caught=caught, missed=missed, followon=followon, unexplained=unexplained)


# ------------------------------------------------------------------------------ plain language
def d(x):  # 'Tue 14 Oct' style
    x = x if isinstance(x, dt.date) else x.date()
    return f"{x.day} {x.strftime('%b')}"


def n(x):
    return f"{int(round(float(x))):,}"


def pct(x, p=1):
    return f"{float(x) * 100:.{p}f}%"


def describe(check, rows, results, con):
    """Return list of alert dicts for one check. One alert per (client, source)."""
    cl = lambda c: G.CLIENTS[c]["name"]
    alerts = []
    meta = {c[0]: c for c in CHECKS}[check]
    sev, owner = meta[4], meta[5]

    def add(client, source, headline, saw, why, fix, evidence=None, severity=None):
        alerts.append(dict(client=client, source=source, check=check, severity=severity or sev, owner=owner,
                           headline=headline, saw=saw, why=why, fix=fix, evidence=evidence or {}))

    if check == "freshness":
        for _, r in rows.iterrows():
            add(r.client_id, r.source,
                f"{SRC_LABEL[r.source]} stopped loading {int(r.days_behind)} days ago",
                f"Newest loaded day is {d(r.latest_date)}. Yesterday was {d(r.expected_date)}.",
                "Dashboards still render, so nothing looks broken. They are serving numbers "
                f"{int(r.days_behind)} days old, and any pacing alert built on them is blind to the last {int(r.days_behind)} days of spend.",
                f"Restart the {SRC_LABEL[r.source]} pipeline and backfill {d(r.latest_date + dt.timedelta(1))} onward. "
                "Add a morning freshness alert so a stall is seen the same day.",
                dict(latest=str(r.latest_date), expected=str(r.expected_date), days_behind=int(r.days_behind)))
    elif check == "completeness":
        for _, r in rows.iterrows():
            if r.kind == "missing_days":
                add(r.client_id, r.source,
                    f"{SRC_LABEL[r.source]} has no data for {int(r.n)} days ({d(r.first_date)} to {d(r.last_date)})",
                    f"Days {d(r.first_date)} to {d(r.last_date)} are absent; the days either side are present.",
                    "Every GA4-based chart reads those days as zero. For GA4's BigQuery export the gap is not "
                    "fixed by re-running a job: Google documents that previous days' exports are not reprocessed.",
                    "Find the cause (see the GA4 export tab), get under the limit, then rebuild the missing days at "
                    "aggregate level from the GA4 Data API and mark them 'rebuilt, not event-level' on the dashboard.",
                    dict(kind="missing_days", n=int(r.n), first=str(r.first_date), last=str(r.last_date)))
            else:
                rc = con.execute("SELECT warehouse_spend / control_spend FROM recon_daily WHERE client_id=? AND platform=? AND date=?",
                                 [r.client_id, r.source, r.first_date]).fetchone()
                short = f"about {pct(1 - rc[0], 0)} short" if rc else "short"
                add(r.client_id, r.source,
                    f"{SRC_LABEL[r.source]} loaded {int(r.n_rows)} of {round(float(r.trailing_avg))} campaigns on {d(r.first_date)}",
                    f"{int(r.n_rows)} campaign rows against a trailing average of {round(float(r.trailing_avg))}.",
                    f"Spend and conversions for {d(r.first_date)} are {short}, so a daily CPA chart shows a one-day spike that never happened.",
                    "Re-pull that day, then add a row-count guard so a partial load blocks the dashboard refresh instead of publishing.",
                    dict(kind="partial_load", day=str(r.first_date), rows=int(r.n_rows), avg=float(r.trailing_avg)))
    elif check == "duplicates":
        for _, r in rows.iterrows():
            share = con.execute("""SELECT SUM(err)/SUM(control_spend) FROM recon_daily WHERE client_id=? AND platform=?""",
                                [r.client_id, r.source]).fetchone()[0] if r.source != "ga4" else None
            extra = f" Period spend is {pct(share)} over the platform total." if share and share > 0.005 else ""
            add(r.client_id, r.source,
                f"{SRC_LABEL[r.source]} rows are in the warehouse twice for {int(r.days_affected)} days",
                f"{int(r.duplicated_keys)} keys appear more than once, {d(r.first_date)} to {d(r.last_date)}.{extra}",
                "Spend and conversions on those days are exactly doubled. CPA looks normal because numerator and "
                "denominator double together; only totals, pacing and ROAS-on-revenue are wrong.",
                "De-dupe on the natural key at load (MERGE, not INSERT), then reload the affected days.",
                dict(keys=int(r.duplicated_keys), days=int(r.days_affected), first=str(r.first_date), last=str(r.last_date),
                     over_pct=float(share) if share else None))
    elif check == "reconcile_total":
        for _, r in rows.iterrows():
            add(r.client_id, r.source,
                f"{SRC_LABEL[r.source]} spend differs from the platform on {int(r.days_off)} of {int(r.days_checked)} days",
                f"Worst day {d(r.worst_day)}: warehouse is {float(r.worst_ratio):.2f}x the platform's number.",
                "This is the symptom, not the cause. The other checks on this client say which of duplicate, partial, "
                "stale or time-shifted it is.",
                "Read the sibling alert for the cause; this one clears when that is fixed.",
                dict(days_off=int(r.days_off), worst_day=str(r.worst_day), worst_ratio=float(r.worst_ratio)))
    elif check == "day_boundary":
        for _, r in rows.iterrows():
            worst = con.execute("SELECT max(abs(err_pct)) FROM recon_daily WHERE client_id=? AND platform=?",
                                [r.client_id, r.source]).fetchone()[0]
            add(r.client_id, r.source,
                f"{SRC_LABEL[r.source]} days are shifted: {int(r.days_off)} of {int(r.days_checked)} days wrong, period total only {pct(abs(r.period_err_pct), 2)} off",
                f"Signed daily errors cancel (net/gross {float(r.net_to_gross):.2f}): spend was moved between days, not lost or added. Worst day is {pct(worst)} off.",
                "A monthly eyeball check passes because the total reconciles. Day-of-week patterns, budget-change "
                "analysis and any daily pacing alert are wrong, and they fire at the wrong time.",
                f"Key the load to the ad account's reporting time zone ({G.CLIENTS[r.client_id]['tz']}) instead of UTC, "
                "store the zone in client config, and reload 28 days.",
                dict(days_off=int(r.days_off), net_to_gross=float(r.net_to_gross), period_err=float(r.period_err_pct), worst=float(worst)))
    elif check == "naming":
        for _, r in rows.iterrows():
            add(r.client_id, r.source,
                f"{int(r.off_convention)} of {int(r.campaigns)} {SRC_LABEL[r.source]} campaigns break the naming convention",
                r.examples,
                "The name is the join key to GA4 (utm_campaign) and what every roll-up parses for objective, audience and geo. "
                "These fall out of every report that parses the name.",
                "Rename in the platform to the convention and add a naming lint to the pre-launch checklist.",
                dict(bad=int(r.off_convention), total=int(r.campaigns), examples=r.examples))
    elif check == "utm_channel":
        for _, r in rows.iterrows():
            add(r.client_id, "ga4",
                f"A paid {SRC_LABEL['meta']} campaign is filed as {r.filed_as}: {pct(r.misfiled_share, 0)} of its sessions",
                f"{r.campaign_name}: {n(r.misfiled_sessions)} of {n(r.total_sessions)} sessions since {d(r.first_date)} are in {r.filed_as}, not {r.expected_channel}.",
                "GA4's Paid Social rule needs a paid-style medium (cp*, ppc, retargeting, paid*). 'social' is an organic medium. "
                "The UTM exists, so a presence-only QA step passes; paid sessions read low while Organic Social looks like it's growing.",
                "Change utm_medium to paid_social on the ad URLs, and re-file the affected dates with a campaign-name override in the model.",
                dict(campaign=r.campaign_name, misfiled=int(r.misfiled_sessions), total=int(r.total_sessions),
                     first=str(r.first_date), last=str(r.last_date)))
    elif check == "utm_values":
        by_client = {}
        for _, r in rows.iterrows():
            by_client.setdefault(r.client_id, []).append(r)
        for cid, rs in by_client.items():
            split = [r for r in rs if r.kind == "spelling_split"]
            off = [r for r in rs if r.kind == "off_allow_list"]
            if split:
                r = split[0]
                add(cid, "ga4", f"utm_source is spelled two ways: {r.detail}",
                    f"{r.subject}: {n(r.sessions)} sessions split across the variants.",
                    "BigQuery string comparison is case-sensitive. A join or GROUP BY on the raw value treats the two "
                    "spellings as different sources, so rows silently drop out of one side.",
                    "Lower-case at ingestion in the staging model and enforce lower-case in the UTM builder.",
                    dict(kind="spelling_split", detail=r.detail, sessions=int(r.sessions)))
            elif off:
                r = off[0]
                add(cid, "ga4", f"A paid campaign uses a medium off the allow-list: {r.detail}",
                    f"{r.subject}: {n(r.sessions)} sessions.",
                    "Medium decides the GA4 channel. An off-list medium is how a paid campaign ends up in an organic channel.",
                    "Fix the ad URL and add the allow-list to the UTM builder.",
                    dict(kind="off_allow_list", detail=r.detail), severity="low")
    elif check == "tag_ratio":
        by_client = {}
        for _, r in rows.iterrows():
            by_client.setdefault((r.client_id, r.source), []).append(r)
        for (cid, src), rs in by_client.items():
            worst = min(float(r.retained) for r in rs)
            names = " | ".join(r.campaign_name for r in rs)
            add(cid, src,
                f"{len(rs)} {SRC_LABEL[src]} campaigns kept {pct(worst, 0)} of their GA4 sessions per click",
                f"{names}. Sessions per click fell from about {float(rs[0].base_ratio):.2f} to {float(rs[0].recent_ratio):.2f}.",
                "Clicks are still being paid for but GA4 sees almost none of the visits. Someone will conclude the campaign stopped "
                "converting. The media is fine; the tag is gone.",
                "Check the GTM container and page_view on the new landing page in DebugView, then add that step to post-launch QA.",
                dict(campaigns=[r.campaign_name for r in rs], retained=worst))
    elif check == "definition":
        for _, r in rows.iterrows():
            if r.kind == "overlapping_action_types":
                add(r.client_id, "meta",
                    f"The conversions field adds {r.detail} overlapping Meta purchase action types: {float(r.inflation):.1f}x too high",
                    f"Dashboard reads {n(r.dashboard_conversions)} conversions; the purchase action type alone is {n(r.canonical_conversions)}.",
                    "Meta returns one purchase under several overlapping action types. Adding them counts the same purchase more "
                    "than once, so ROAS and CPA are wrong by the same factor and budget moves are made on the wrong number.",
                    "Pin each metric to a single action type and add a registry test that rejects overlapping members.",
                    dict(kind="overlap", dashboard=int(r.dashboard_conversions), canonical=int(r.canonical_conversions), inflation=float(r.inflation)))
            else:
                add(r.client_id, "meta", f"The dashboard asks Meta for a window it no longer returns: {r.detail}",
                    "The configured attribution window never appears in the warehouse.",
                    "Meta stopped returning the 7d_view and 28d_view windows on 12 Jan 2026. A field built on one reads from a "
                    "column with nothing in it, so conversions for this client cannot be right.",
                    "Move the field to the registry standard (7d_click) and annotate the change on 12 Jan 2026 so year-over-year views stay honest.",
                    dict(kind="window", detail=r.detail))
    elif check == "ga4_headroom":
        for _, r in rows.iterrows():
            lead = None
            if r.first_missing_export_date is not None and str(r.first_missing_export_date) != "NaT":
                lead = (r.first_missing_export_date - r.first_breach_date).days
            tail = (f" The first missing export day was {d(r.first_missing_export_date)}, {lead} days later." if lead else "")
            add(r.client_id, "ga4",
                f"GA4 reached 85% of the 1M events/day export limit on {d(r.first_breach_date)}",
                f"7-day average peaked at {n(r.peak_avg7)} events/day.{tail}",
                "Standard properties that consistently exceed the limit get the daily BigQuery export paused, and previous days are not "
                "reprocessed. Google emails property editors and administrators. This check tells the people who own the dashboard.",
                "Exclude low-value events or streams from the export, or move the property to GA4 360, before the pause.",
                dict(first_breach=str(r.first_breach_date), peak=float(r.peak_avg7), lead_days=lead))
    return alerts


# ------------------------------------------------------------------------------ chart data
def chart_data(con):
    ch = {}
    # GA4 export vs limit
    rows = con.execute("""SELECT client_id, event_date, events_collected, events_exported IS NOT NULL AS exported
                          FROM raw_ga4_export_log ORDER BY client_id, event_date""").fetchall()
    ch["ga4"] = {c: [dict(date=str(r[1]), collected=r[2], exported=bool(r[3])) for r in rows if r[0] == c] for c in G.CLIENTS}
    # day shift (bright meta)
    rows = con.execute("""SELECT date, control_spend, warehouse_spend, err_pct FROM recon_daily
                          WHERE client_id='bright' AND platform='meta' ORDER BY date""").fetchall()
    ch["shift"] = [dict(date=str(r[0]), control=round(r[1], 2), warehouse=round(r[2], 2), err=round(r[3], 4)) for r in rows]
    ch["hours"] = [round(float(x), 4) for x in G.HOUR_W]
    # channel split for the mis-tagged campaign
    rows = con.execute("""SELECT event_date, channel, SUM(sessions) FROM stg_ga4_channel
                          WHERE client_id='alder' AND session_campaign='alder_meta_con_202609_lal1_us'
                          GROUP BY event_date, channel ORDER BY event_date""").fetchall()
    days = {}
    for dte, chn, s in rows:
        days.setdefault(str(dte), {})[chn] = int(s)
    ch["channel"] = [dict(date=k, paid=v.get("Paid Social", 0), organic=v.get("Organic Social", 0)) for k, v in sorted(days.items())]
    # per-channel sessions for alder, whole account, to show the knock-on
    rows = con.execute("""SELECT channel, SUM(sessions) FILTER (WHERE event_date < DATE '2026-10-12'),
                                  SUM(sessions) FILTER (WHERE event_date >= DATE '2026-10-12')
                          FROM stg_ga4_channel WHERE client_id='alder' GROUP BY channel ORDER BY 1""").fetchall()
    ch["alder_channels"] = [dict(channel=r[0], before=int(r[1] or 0), after=int(r[2] or 0)) for r in rows]
    # action types, de-duplicated
    rows = con.execute("""SELECT client_id, action_type, SUM(value) FROM
                          (SELECT DISTINCT client_id, date_start, campaign_id, action_type, attribution_window, value FROM raw_meta_actions)
                          WHERE action_type IN ('purchase','omni_purchase','offsite_conversion.fb_pixel_purchase')
                          GROUP BY 1,2""").fetchall()
    ch["actions"] = {}
    for c, a, v in rows:
        ch["actions"].setdefault(c, {})[a] = int(v)
    # click -> session ratio for bright gads campaigns
    rows = con.execute("""SELECT s.date, s.campaign_name, SUM(s.clicks), COALESCE(SUM(g.sessions),0)
                          FROM stg_spend s LEFT JOIN raw_ga4_traffic g
                            ON g.client_id=s.client_id AND g.session_campaign=s.campaign_name AND g.event_date=s.date
                          WHERE s.client_id='bright' AND s.platform='gads' GROUP BY 1,2 ORDER BY 1,2""").fetchall()
    ratio = {}
    for dte, camp, cl, se in rows:
        ratio.setdefault(camp, []).append(dict(date=str(dte), ratio=round(se / cl, 3) if cl else None))
    ch["ratio"] = ratio
    rows = con.execute("""SELECT r.client_id, r.platform, r.metric, r.action_types, r.attribution_window,
                                 EXISTS (SELECT 1 FROM raw_meta_actions a WHERE a.client_id=r.client_id
                                         AND a.attribution_window=r.attribution_window) AS window_in_data
                          FROM metric_registry r ORDER BY r.client_id, r.platform""").fetchall()
    ch["registry"] = [dict(client=r[0], platform=r[1], metric=r[2], action_types=r[3], window=r[4], window_in_data=bool(r[5])) for r in rows]
    return ch


def channel_unit_tests(frames):
    con = connect_memory(frames)
    con.execute("DELETE FROM raw_ga4_traffic")
    cases = [
        ("facebook", "paid_social", "Paid Social"), ("facebook", "cpc", "Paid Social"), ("facebook", "ppc", "Paid Social"),
        ("facebook", "retargeting", "Paid Social"), ("facebook", "cpm", "Paid Social"), ("facebook", "paid-social", "Paid Social"),
        ("Facebook", "Paid_Social", "Paid Social"), ("instagram", "paidsocial", "Paid Social"),
        ("facebook", "social", "Organic Social"), ("instagram", "social", "Organic Social"), ("facebook", "referral", "Organic Social"),
        ("tiktok", "social-media", "Organic Social"), ("facebook", "display", "Display"),
        ("google", "cpc", "Paid Search"), ("google", "ppc", "Paid Search"), ("bing", "cpc", "Paid Search"),
        ("google", "organic", "Organic Search"), ("dv360", "display", "Display"), ("dv360", "cpm", "Display"),
        ("newsletter", "email", "Email"), ("(direct)", "(none)", "Direct"), ("(direct)", "(not set)", "Direct"),
        ("dv360", "programmatic", "Unassigned"), ("somesite.com", "referral", "Referral"),
    ]
    for i, (s, m, _) in enumerate(cases):
        con.execute("INSERT INTO raw_ga4_traffic VALUES ('t', DATE '2026-10-01', ?, ?, ?, 1, 0, 0)", [s, m, f"t{i}"])
    got = dict(con.execute("SELECT session_campaign, channel FROM stg_ga4_channel").fetchall())
    res = [dict(source=s, medium=m, expected=e, got=got[f"t{i}"], ok=got[f"t{i}"] == e) for i, (s, m, e) in enumerate(cases)]
    return res


def transpile(sql):
    out = {}
    for dialect in ("bigquery", "snowflake"):
        try:
            stmts = sqlglot.transpile(sql, read="duckdb", write=dialect, pretty=True)
            out[dialect] = ";\n\n".join(stmts) + ";"
        except Exception as e:  # noqa: BLE001
            out[dialect] = f"-- sqlglot could not translate this file automatically: {type(e).__name__}"
    return out


# ------------------------------------------------------------------------------ triage
REQUESTS = [
    dict(id="R1", client="alder",  title="Exec dashboard ROAS looks too good this week. Can you check it?", tier="A", due=1, dashboards=3, wrong=True,  effort=3,  link=("alder", "definition")),
    dict(id="R2", client="bright", title="Paid search conversions fell off a cliff. Did the campaign stop?", tier="A", due=1, dashboards=2, wrong=True,  effort=2,  link=("bright", "tag_ratio")),
    dict(id="R3", client="cobalt", title="GA4 shows zeros for 14 to 19 Oct. Can we backfill?",              tier="A", due=2, dashboards=4, wrong=True,  effort=6,  link=("cobalt", "completeness")),
    dict(id="R4", client="bright", title="Pre-launch QA for the new landing page, going live Thursday",     tier="A", due=2, dashboards=0, wrong=False, effort=2,  link=None),
    dict(id="R5", client="delta",  title="Ad hoc: Meta vs Google CPA, September vs October",                tier="B", due=3, dashboards=0, wrong=False, effort=4,  link=None),
    dict(id="R6", client="cobalt", title="Add DV360 CTV completed views to the weekly dashboard",           tier="B", due=9, dashboards=1, wrong=False, effort=10, link=None),
    dict(id="R7", client="delta",  title="Standardise Q4 campaign naming across all platforms",             tier="B", due=10, dashboards=0, wrong=False, effort=5, link=("delta", "naming")),
    dict(id="R8", client="alder",  title="New metric: new-customer rate. Scope the data sources",           tier="A", due=14, dashboards=1, wrong=False, effort=6, link=None),
]
TIER = {"A": 1.0, "B": 0.6, "C": 0.3}


def triage():
    out = []
    for r in REQUESTS:
        parts = dict(
            urgency=max(0.0, min(1.0, 1 - r["due"] / 14)),
            blast=min(r["dashboards"] / 4, 1.0),
            integrity=1.0 if r["wrong"] else 0.0,
            tier=TIER[r["tier"]],
            cheap=max(0.0, min(1.0, 1 - r["effort"] / 12)),
        )
        s = 0.30 * parts["urgency"] + 0.25 * parts["blast"] + 0.25 * parts["integrity"] + 0.10 * parts["tier"] + 0.10 * parts["cheap"]
        sla = "Today" if s >= 0.70 else "Within 48 hours" if s >= 0.45 else "This week's batch"
        if r["due"] <= 2 and sla == "This week's batch":  # hard rule: a near deadline is never batched
            sla = "Within 48 hours"
        out.append(dict(r, parts={k: round(v, 3) for k, v in parts.items()}, score=round(s, 3), sla=sla))
    out.sort(key=lambda x: -x["score"])
    return out


# ------------------------------------------------------------------------------ main
def one_run(seed, faults):
    frames = G.build(seed, faults=faults)
    con = connect_memory(frames)
    res = run_all(con)
    return frames, con, res


def validate():
    clean_alerts, clean_cells = 0, 0
    per_seed_clean = []
    for s in range(1, 21):
        _, _, res = one_run(s, False)
        t = tuples(res)
        clean_alerts += len(t)
        clean_cells += N_CELLS
        per_seed_clean.append(len(t))
    caught = missed = unexpl = runs_ok = 0
    detail = []
    for s in range(101, 121):
        _, _, res = one_run(s, True)
        sc = score(tuples(res))
        caught += len(sc["caught"])
        missed += len(sc["missed"])
        unexpl += len(sc["unexplained"])
        ok = not sc["missed"] and not sc["unexplained"]
        runs_ok += ok
        detail.append(dict(seed=s, caught=len(sc["caught"]), missed=[list(m) for m in sc["missed"]],
                           unexplained=[list(u) for u in sc["unexplained"]]))
    return dict(clean=dict(runs=20, cells=clean_cells, alerts=clean_alerts, per_seed=per_seed_clean),
                faulted=dict(runs=20, faults=len(G.FAULTS) * 20, caught=caught, missed=missed, unexplained=unexpl,
                             runs_perfect=runs_ok, detail=detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    frames, con, res = one_run(args.seed, not args.clean)
    t = tuples(res)
    sc = score(t, faults=not args.clean)

    if args.clean:
        print(f"clean run seed={args.seed}: {len(t)} alerts over {N_CELLS} checks", sorted(t))
        return

    print(f"seed {args.seed}: {len(sc['caught'])}/{len(G.FAULTS)} faults caught, {len(sc['followon'])} follow-on, "
          f"{len(sc['unexplained'])} unexplained, {len(sc['missed'])} missed")
    for k in sc["unexplained"]:
        print("  UNEXPLAINED", k)
    for k in sc["missed"]:
        print("  MISSED", k)

    # alerts
    alerts = []
    for cid, fname, *_ in CHECKS:
        alerts += describe(cid, res[cid], res, con)
    primary = {(f["client"], f["source"], f["primary"]): f for f in G.FAULTS}
    follow = {}
    for f in G.FAULTS:
        for c in f["also_ok"]:
            follow[(f["client"], f["source"], c)] = f["id"]
    sev_rank = {"high": 0, "med": 1, "low": 2}
    for a in alerts:
        key = (a["client"], a["source"], a["check"])
        if key in primary:
            a["status"] = "caught"; a["fault"] = primary[key]["id"]; a["fault_text"] = primary[key]["what"]
        elif key in set(G.EARLY_WARNING):
            a["status"] = "early"; a["fault"] = "F11"
        elif key in follow:
            a["status"] = "followon"; a["fault"] = follow[key]
        else:
            a["status"] = "unexplained"; a["fault"] = None
    # reconcile follow-ons are keyed on the platform source; match by client+check when source differs
    order = {"caught": 0, "early": 1, "followon": 2, "unexplained": 3}
    alerts.sort(key=lambda a: (order[a["status"]], sev_rank[a["severity"]], a["client"], a["check"]))
    for i, a in enumerate(alerts, 1):
        a["id"] = f"A{i:02d}"
    byk = {(a["client"], a["check"]): a["id"] for a in alerts}

    # matrix
    matrix = {}
    for cid in CHECK_IDS:
        matrix[cid] = {}
        for c in G.CLIENTS:
            hits = [a for a in alerts if a["client"] == c and a["check"] == cid]
            if not hits:
                st = "pass"
            elif any(h["status"] == "caught" for h in hits):
                st = "fail"
            elif any(h["status"] == "early" for h in hits):
                st = "early"
            else:
                st = "followon"
            matrix[cid][c] = dict(status=st, alerts=[h["id"] for h in hits], sources=sorted({h["source"] for h in hits}))

    tri = triage()
    for r in tri:
        r["alert"] = byk.get(r["link"]) if r["link"] else None
        r["client_name"] = G.CLIENTS[r["client"]]["name"]
        r.pop("link")

    sql_files = []
    for fname in ["00_staging.sql"] + [c[1] for c in CHECKS]:
        txt = (SQL / fname).read_text()
        sql_files.append(dict(file=fname, id=fname[3:-4], sql=txt, **transpile(txt)))

    out = dict(
        meta=dict(as_of=str(G.AS_OF), first_day=str(G.DAYS[0]), last_day=str(G.DAYS[-1]), days=G.N_DAYS, seed=args.seed,
                  cells=N_CELLS, faults=len(G.FAULTS), caught=len(sc["caught"]), missed=len(sc["missed"]),
                  followon=len(sc["followon"]), unexplained=len(sc["unexplained"]),
                  rows={k: len(v) for k, v in frames.items() if k.startswith("raw_") or k.startswith("ctl_")}),
        clients=[dict(id=k, **{x: v[x] for x in ("name", "vertical", "tz")}) for k, v in G.CLIENTS.items()],
        checks=[dict(id=c[0], file=c[1], label=c[2], purpose=c[3], severity=c[4], owner=c[5]) for c in CHECKS],
        matrix=matrix, alerts=alerts, faults=G.FAULTS, charts=chart_data(con), triage=tri,
        unit_tests=channel_unit_tests(G.build(args.seed, faults=False)), sql=sql_files,
    )
    if args.validate:
        out["validation"] = validate()
        v = out["validation"]
        print(f"clean:   {v['clean']['runs']} seeds x {N_CELLS} checks -> {v['clean']['alerts']} alerts")
        print(f"faulted: {v['faulted']['runs']} seeds -> caught {v['faulted']['caught']}/{v['faulted']['faults']}, "
              f"unexplained {v['faulted']['unexplained']}, perfect runs {v['faulted']['runs_perfect']}/20")
    (ROOT / "site" / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "site" / "data" / "results.js").write_text("window.RESULTS = " + json.dumps(out, default=str) + ";\n")
    (ROOT / "results.json").write_text(json.dumps(out, default=str, indent=1))
    print("wrote site/data/results.js")


if __name__ == "__main__":
    main()
