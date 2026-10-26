#!/usr/bin/env python3
"""
Synthetic media warehouse for the Media Data Health Check.

EVERYTHING here is generated. No client data, no real accounts.
The generator builds four fictional clients across Meta, Google Ads, DV360 and
GA4, then (optionally) injects 11 known faults. The fault list is the answer key
the checks are scored against.

    python3 gen_data.py --seed 42            # faulted warehouse -> media.duckdb
    python3 gen_data.py --seed 42 --clean    # same noise, no faults
"""
import argparse
import datetime as dt
import json
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd

START = dt.date(2026, 9, 28)
N_DAYS = 28
AS_OF = dt.date(2026, 10, 26)
DAYS = [START + dt.timedelta(d) for d in range(N_DAYS)]  # 9/28 .. 10/25
LAST = DAYS[-1]

CLIENTS = {
    "alder":  {"name": "Alder & Finch",     "vertical": "Retail",     "tz": "America/Los_Angeles", "scale": 1.00, "events": 310_000},
    "bright": {"name": "Brightline Health", "vertical": "Healthcare", "tz": "America/Los_Angeles", "scale": 0.80, "events": 140_000},
    "cobalt": {"name": "Cobalt Mobile",     "vertical": "Mobile app", "tz": "America/Chicago",     "scale": 1.40, "events": 720_000},
    "delta":  {"name": "Delta Pantry",      "vertical": "CPG",        "tz": "America/Denver",      "scale": 0.90, "events": 220_000},
}
PLATFORMS = ("meta", "gads", "dv360")

HOUR_W = np.array([2, 1.5, 1, .8, .8, 1, 1.5, 2.5, 3.5, 4.2, 4.8, 5, 5, 5, 5.2, 5.5,
                   5.8, 6.2, 6.8, 7.5, 8, 8.2, 7, 4.5], dtype=float)
HOUR_W /= HOUR_W.sum()
WEEKDAY = [1.00, 1.00, 1.02, 1.05, 1.10, 0.90, 0.86]  # Mon..Sun

PLAT = {  # synthetic behaviour per platform
    "meta":  {"cpc": 1.15, "ctr": 0.011, "cvr": 0.021, "sess": 0.72, "src": "facebook", "med": "paid_social"},
    "gads":  {"cpc": 1.90, "ctr": 0.045, "cvr": 0.034, "sess": 0.88, "src": "google",   "med": "cpc"},
    "dv360": {"cpc": 2.60, "ctr": 0.0035, "cvr": 0.006, "sess": 0.50, "src": "dv360",    "med": "display"},
}

GA4_EVENT_LIMIT = 1_000_000


def campaign_plan(c):
    return {
        "meta": [(f"{c}_meta_con_202609_lal1_us", 900), (f"{c}_meta_con_202609_rtg_us", 500),
                 (f"{c}_meta_trf_202610_intr_us", 420), (f"{c}_meta_awr_202610_bro_us", 650)],
        "gads": [(f"{c}_gads_con_202609_brand_us", 600), (f"{c}_gads_con_202609_generic_us", 800),
                 (f"{c}_gads_con_202610_pmax_us", 700), (f"{c}_gads_trf_202610_shop_us", 350)],
        "dv360": [(f"{c}_dv360_awr_202609_ctv_us", 750), (f"{c}_dv360_awr_202610_prog_us", 540)],
    }


# --------------------------------------------------------------------------------------
# The answer key. Each fault names the check that must catch it (primary) and the checks
# that are ALLOWED to also fire because they are honest knock-on symptoms (also_ok).
# --------------------------------------------------------------------------------------
FAULTS = [
    dict(id="F01", client="alder",  source="meta",  primary="duplicates",
         also_ok=["reconcile_total"],
         what="A Meta load was re-run for two days with no de-dupe, so those days are in the warehouse twice."),
    dict(id="F02", client="alder",  source="ga4",   primary="utm_channel",
         also_ok=["utm_values"],
         what="One paid Meta campaign was relaunched with utm_medium=social, so GA4 files its sessions under Organic Social."),
    dict(id="F03", client="alder",  source="meta",  primary="definition",
         also_ok=[],
         what="The conversions field in the dashboard sums three overlapping Meta purchase action types."),
    dict(id="F04", client="bright", source="gads",  primary="tag_ratio",
         also_ok=[],
         what="A new landing page shipped without the GTM container, so two Google Ads campaigns keep clicking but stop producing GA4 sessions."),
    dict(id="F05", client="bright", source="meta",  primary="day_boundary",
         also_ok=["reconcile_total"],
         what="The Meta pipeline groups hourly spend by UTC date, but the ad account reports in Pacific time."),
    dict(id="F06", client="cobalt", source="dv360", primary="freshness",
         also_ok=["reconcile_total"],
         what="The DV360 pipeline stalled; the last three days never loaded."),
    dict(id="F07", client="cobalt", source="meta",  primary="definition",
         also_ok=[],
         what="The dashboard asks for the 7d_view attribution window, which Meta stopped returning on 12 Jan 2026."),
    dict(id="F08", client="delta",  source="gads",  primary="completeness",
         also_ok=["reconcile_total"],
         what="A partial Google Ads load: on one day only two of four campaigns arrived."),
    dict(id="F09", client="delta",  source="gads",  primary="naming",
         also_ok=[],
         what="Two campaigns were launched with hand-typed names that break the naming convention."),
    dict(id="F10", client="delta",  source="ga4",   primary="utm_values",
         also_ok=[],
         what="utm_source is spelled Facebook on some days and facebook on others; a case-sensitive join treats them as different sources."),
    dict(id="F11", client="cobalt", source="ga4",   primary="completeness",
         also_ok=["ga4_headroom"],
         what="The property crossed 1,000,000 events/day, the standard-property BigQuery export paused, and the missed days were never reprocessed."),
]
# ga4_headroom is an early-warning check: it is expected to fire for cobalt as a leading indicator of F11.
EARLY_WARNING = [("cobalt", "ga4", "ga4_headroom")]


def _utc_dates(tzname):
    """utc_date[k][h] for local day k (k=0 is START-1) and hour h."""
    tz = ZoneInfo(tzname)
    out = []
    for k in range(N_DAYS + 2):
        d = START + dt.timedelta(k - 1)
        row = []
        for h in range(24):
            loc = dt.datetime(d.year, d.month, d.day, h, tzinfo=tz)
            row.append(loc.astimezone(dt.timezone.utc).date())
        out.append(row)
    return out


def build(seed, faults=True):
    rng = np.random.default_rng(seed)
    frng = np.random.default_rng(seed + 10_000)  # fault noise stream, independent of base data

    meta_ins, meta_act, gads, dv, ga4, ctl, export_log = [], [], [], [], [], [], []
    registry = []

    for ci, (cid, cfg) in enumerate(CLIENTS.items()):
        utc_map = _utc_dates(cfg["tz"])
        for plat in PLATFORMS:
            P = PLAT[plat]
            for idx, (cname, budget) in enumerate(campaign_plan(cid)[plat]):
                # --- hourly spend (local time) for START-1 .. LAST+1 -------------------
                step_day = int(rng.integers(9, 19))
                step_f = float(rng.choice([1.0, 1.0, 1.25, 1.4]))
                hourly_spend = np.zeros((N_DAYS + 2, 24))
                hourly_clicks = np.zeros((N_DAYS + 2, 24))
                hourly_impr = np.zeros((N_DAYS + 2, 24))
                for k in range(N_DAYS + 2):
                    d = START + dt.timedelta(k - 1)
                    daily = budget * cfg["scale"] * WEEKDAY[d.weekday()] * (step_f if (k - 1) >= step_day else 1.0)
                    daily *= float(rng.lognormal(0, 0.07))
                    cpc = P["cpc"] * float(rng.lognormal(0, 0.06))
                    ctr = P["ctr"] * float(rng.lognormal(0, 0.05))
                    hs = daily * HOUR_W * rng.lognormal(0, 0.12, 24)
                    hourly_spend[k] = hs
                    hourly_clicks[k] = hs / cpc
                    hourly_impr[k] = hs / cpc / ctr

                def local(arr):  # 28 local days
                    return arr[1:N_DAYS + 1].sum(axis=1)

                def utc(arr):  # 28 utc days built from the same hours
                    acc = {d: 0.0 for d in DAYS}
                    for k in range(N_DAYS + 2):
                        for h in range(24):
                            u = utc_map[k][h]
                            if u in acc:
                                acc[u] += arr[k, h]
                    return np.array([acc[d] for d in DAYS])

                t_spend, t_clicks, t_impr = local(hourly_spend), local(hourly_clicks), local(hourly_impr)
                w_spend, w_clicks, w_impr = t_spend, t_clicks, t_impr  # what the warehouse stores

                # F05: bright meta pipeline groups by UTC date
                if faults and cid == "bright" and plat == "meta":
                    w_spend, w_clicks, w_impr = utc(hourly_spend), utc(hourly_clicks), utc(hourly_impr)

                cvr_day = P["cvr"] * rng.lognormal(0, 0.10, N_DAYS)
                conv = np.round(w_clicks * cvr_day)

                name_out = cname
                # F09: delta gads - two hand-typed names
                if faults and cid == "delta" and plat == "gads" and idx == 1:
                    name_out = "Delta Fall Promo - Search v2"
                if faults and cid == "delta" and plat == "gads" and idx == 3:
                    name_out = "DELTA_BRAND_Search_Oct"

                camp_id = {"meta": f"238{ci}5{idx:03d}", "gads": f"{ci + 1}7{idx:02d}00", "dv360": f"li-{ci + 1}{idx:03d}"}[plat]

                for i, d in enumerate(DAYS):
                    rec = dict(client_id=cid, date=d, campaign_id=camp_id, campaign_name=name_out,
                               spend=round(float(w_spend[i]), 2), impressions=int(round(w_impr[i])),
                               clicks=int(round(w_clicks[i])))
                    if plat == "meta":
                        meta_ins.append(dict(client_id=cid, date_start=d, campaign_id=camp_id,
                                             campaign_name=name_out, spend=rec["spend"],
                                             impressions=rec["impressions"], clicks=rec["clicks"]))
                        n = int(conv[i])
                        for atype, val in (("purchase", n), ("omni_purchase", n),
                                           ("offsite_conversion.fb_pixel_purchase", int(round(n * 0.9))),
                                           ("link_click", int(round(rec["clicks"] * 0.92))),
                                           ("landing_page_view", int(round(rec["clicks"] * 0.74))),
                                           ("add_to_cart", int(round(n * 3.1)))):
                            meta_act.append(dict(client_id=cid, date_start=d, campaign_id=camp_id,
                                                 action_type=atype, attribution_window="7d_click", value=val))
                    elif plat == "gads":
                        gads.append(dict(client_id=cid, segments_date=d, campaign_id=camp_id,
                                         campaign_name=name_out, cost_micros=int(round(rec["spend"] * 1e6)),
                                         impressions=rec["impressions"], clicks=rec["clicks"],
                                         conversions=round(float(conv[i]), 2)))
                    else:
                        dv.append(dict(client_id=cid, date=d, line_item_id=camp_id, line_item=name_out,
                                       media_cost_usd=rec["spend"], impressions=rec["impressions"],
                                       clicks=rec["clicks"], total_conversions=int(conv[i])))

                    # --- GA4 sessions come from the TRUE clicks, independent of warehouse faults
                    sess = int(round(t_clicks[i] * P["sess"] * float(rng.lognormal(0, 0.04))))
                    src, med = P["src"], P["med"]
                    if faults:
                        # F04: bright gads, two campaigns lose the tag from day 17
                        if cid == "bright" and plat == "gads" and idx in (1, 2) and i >= 16:
                            sess = int(round(sess * 0.06))
                        # F02: alder meta relaunch tagged social from day 15
                        if cid == "alder" and plat == "meta" and idx == 0 and i >= 14:
                            med = "social"
                        # F10: delta - Facebook capitalised on some days
                        if cid == "delta" and plat == "meta" and i % 5 == 0:
                            src = "Facebook"
                    ke = int(round(sess * 0.024 * float(rng.lognormal(0, 0.1))))
                    ga4.append(dict(client_id=cid, event_date=d, session_source=src, session_medium=med,
                                    session_campaign=name_out, sessions=sess, key_events=ke,
                                    revenue=round(ke * 68.0 * float(rng.lognormal(0, 0.08)), 2)))

                    # --- control totals = the platform UI, with a whisper of reporting noise
                    ctl.append(dict(client_id=cid, platform=plat, date=d, _camp=camp_id,
                                    spend=float(t_spend[i]), clicks=float(t_clicks[i])))

        # --- organic / baseline GA4 rows and export volume --------------------------------
        for i, d in enumerate(DAYS):
            s = cfg["scale"]
            base = [("google", "organic", "(organic)", 2600 * s), ("(direct)", "(none)", "(direct)", 1900 * s),
                    ("instagram", "social", "(organic)", 240 * s)]
            if d.weekday() in (1, 3, 5):
                base.append(("newsletter", "email", "fall_newsletter", 320 * s))
            for src, med, camp, n in base:
                sess = int(round(n * float(rng.lognormal(0, 0.06))))
                ke = int(round(sess * 0.02))
                ga4.append(dict(client_id=cid, event_date=d, session_source=src, session_medium=med,
                                session_campaign=camp, sessions=sess, key_events=ke,
                                revenue=round(ke * 68.0, 2)))
            collected = cfg["events"] * float(rng.lognormal(0, 0.03))
            if cid == "cobalt" and faults:   # the ramp toward the export limit IS fault F11
                dd = i + 1
                collected = (720_000 + 24_000 * (dd - 1)) * float(rng.lognormal(0, 0.012)) if dd <= 22 \
                    else 740_000 * float(rng.lognormal(0, 0.03))
            export_log.append(dict(client_id=cid, event_date=d, events_collected=int(collected),
                                   events_exported=int(collected)))

        # --- metric registry (what the dashboards are configured to read) --------------------
        registry.append(dict(client_id=cid, metric="conversions", platform="meta",
                             action_types="purchase", attribution_window="7d_click"))
        registry.append(dict(client_id=cid, metric="conversions", platform="gads",
                             action_types="conversions", attribution_window="n/a"))

    # ---------------------------------------------------------------- build frames
    df = {k: pd.DataFrame(v) for k, v in dict(raw_meta_insights=meta_ins, raw_meta_actions=meta_act,
          raw_gads_stats=gads, raw_dv360_stats=dv, raw_ga4_traffic=ga4, raw_ga4_export_log=export_log,
          metric_registry=registry).items()}
    ctl_df = pd.DataFrame(ctl)
    ctl_df = ctl_df.groupby(["client_id", "platform", "date"], as_index=False).agg(spend=("spend", "sum"), clicks=("clicks", "sum"))
    ctl_df["spend"] = (ctl_df["spend"] * frng.normal(1.0, 0.0012, len(ctl_df))).round(2)
    ctl_df["clicks"] = ctl_df["clicks"].round().astype(int)
    df["ctl_platform_totals"] = ctl_df

    if faults:
        # F01 alder meta duplicates (day 9 and 10)
        dup_days = {DAYS[9], DAYS[10]}
        m = df["raw_meta_insights"]
        a = df["raw_meta_actions"]
        df["raw_meta_insights"] = pd.concat([m, m[(m.client_id == "alder") & m.date_start.isin(dup_days)]], ignore_index=True)
        df["raw_meta_actions"] = pd.concat([a, a[(a.client_id == "alder") & a.date_start.isin(dup_days)]], ignore_index=True)

        # F03 alder dashboard sums three overlapping purchase action types
        r = df["metric_registry"]
        r.loc[(r.client_id == "alder") & (r.platform == "meta"), "action_types"] = \
            "purchase,omni_purchase,offsite_conversion.fb_pixel_purchase"
        # F07 cobalt dashboard asks for a window Meta no longer returns
        r.loc[(r.client_id == "cobalt") & (r.platform == "meta"), "attribution_window"] = "7d_view"
        df["metric_registry"] = r

        # F06 cobalt dv360 stalls: last 3 days never load
        d6 = df["raw_dv360_stats"]
        df["raw_dv360_stats"] = d6[~((d6.client_id == "cobalt") & (d6.date > DAYS[-4]))]

        # F08 delta gads partial load on day 21 (2 of 4 campaigns)
        g = df["raw_gads_stats"]
        keep_ids = set(g[(g.client_id == "delta")].campaign_id.unique()[:2])
        drop = (g.client_id == "delta") & (g.segments_date == DAYS[20]) & (~g.campaign_id.isin(keep_ids))
        df["raw_gads_stats"] = g[~drop]

        # F11 cobalt GA4 export paused d17..d22 (rows never arrive; events never exported)
        gap = set(DAYS[16:22])
        t = df["raw_ga4_traffic"]
        df["raw_ga4_traffic"] = t[~((t.client_id == "cobalt") & t.event_date.isin(gap))]
        e = df["raw_ga4_export_log"]
        e.loc[(e.client_id == "cobalt") & e.event_date.isin(gap), "events_exported"] = pd.NA
        df["raw_ga4_export_log"] = e

    df["client_config"] = pd.DataFrame([dict(client_id=k, name=v["name"], vertical=v["vertical"], timezone=v["tz"])
                                        for k, v in CLIENTS.items()])
    df["params"] = pd.DataFrame([dict(as_of=AS_OF, ga4_event_limit=GA4_EVENT_LIMIT)])
    return df


def write_db(df, path):
    con = duckdb.connect(path)
    for name, frame in df.items():
        con.register("_f", frame)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _f")
        con.unregister("_f")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--clean", action="store_true", help="no injected faults")
    ap.add_argument("--out", default="media.duckdb")
    args = ap.parse_args()
    import os
    if os.path.exists(args.out):
        os.remove(args.out)
    frames = build(args.seed, faults=not args.clean)
    write_db(frames, args.out)
    json.dump(FAULTS, open("faults.json", "w"), indent=2)
    print(f"wrote {args.out}  seed={args.seed}  faults={'off' if args.clean else len(FAULTS)}  "
          + "  ".join(f"{k}={len(v)}" for k, v in frames.items()))
