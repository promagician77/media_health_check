-- 00_staging: one grain for spend, a re-implementation of GA4's default channel rules,
-- and the daily reconciliation view every spend check reads from.

CREATE OR REPLACE VIEW stg_spend AS
SELECT client_id, 'meta' AS platform, date_start AS date, campaign_id, campaign_name,
       spend, impressions, clicks
FROM raw_meta_insights
UNION ALL
SELECT client_id, 'gads', segments_date, campaign_id, campaign_name,
       cost_micros / 1000000.0, impressions, clicks            -- Google Ads reports micros
FROM raw_gads_stats
UNION ALL
SELECT client_id, 'dv360', date, line_item_id, line_item,
       media_cost_usd, impressions, clicks
FROM raw_dv360_stats;

-- GA4 default channel group, subset, re-implemented from Google's published rules
--   Paid Social    : source is a social site AND medium matches ^(.*cp.*|ppc|retargeting|paid.*)$
--   Organic Social : source is a social site OR medium in (social, social-network, social-media, sm, ...)
-- Channel definitions are not case sensitive, so everything is lower()-ed first.
-- The social-site and search-site lists are abbreviated; Google's full lists are longer.
CREATE OR REPLACE VIEW stg_ga4_channel AS
SELECT t.*,
  CASE
    WHEN lower(session_source) = '(direct)' AND lower(session_medium) IN ('(none)', '(not set)')
         THEN 'Direct'
    WHEN regexp_matches(lower(session_source), '^(facebook|fb|instagram|ig|linkedin|twitter|t\.co|x\.com|pinterest|reddit|tiktok|snapchat)$')
         AND regexp_matches(lower(session_medium), '^(.*cp.*|ppc|retargeting|paid.*)$')
         THEN 'Paid Social'
    WHEN regexp_matches(lower(session_source), '^(google|bing|yahoo|duckduckgo)$')
         AND regexp_matches(lower(session_medium), '^(.*cp.*|ppc|retargeting|paid.*)$')
         THEN 'Paid Search'
    WHEN lower(session_medium) IN ('display', 'banner', 'expandedbanner', 'interstitial', 'cpm')
         THEN 'Display'
    WHEN regexp_matches(lower(session_source), '^(facebook|fb|instagram|ig|linkedin|twitter|t\.co|x\.com|pinterest|reddit|tiktok|snapchat)$')
         OR lower(session_medium) IN ('social', 'social-network', 'social-media', 'sm', 'social network', 'social media')
         THEN 'Organic Social'
    WHEN lower(session_medium) IN ('email', 'e-mail', 'e_mail', 'e mail') THEN 'Email'
    WHEN lower(session_medium) IN ('referral', 'app', 'link') THEN 'Referral'
    WHEN regexp_matches(lower(session_source), '^(google|bing|yahoo|duckduckgo)$')
         OR lower(session_medium) = 'organic' THEN 'Organic Search'
    ELSE 'Unassigned'
  END AS channel
FROM raw_ga4_traffic t;

-- Warehouse vs platform control totals, per client / platform / day.
-- Only days up to yesterday are judged.
CREATE OR REPLACE VIEW recon_daily AS
WITH wh AS (
  SELECT client_id, platform, date, SUM(spend) AS warehouse_spend
  FROM stg_spend GROUP BY client_id, platform, date
)
SELECT c.client_id, c.platform, c.date,
       c.spend                              AS control_spend,
       COALESCE(w.warehouse_spend, 0)       AS warehouse_spend,
       COALESCE(w.warehouse_spend, 0) - c.spend AS err,
       (COALESCE(w.warehouse_spend, 0) - c.spend) / c.spend AS err_pct
FROM ctl_platform_totals c
LEFT JOIN wh w
  ON w.client_id = c.client_id AND w.platform = c.platform AND w.date = c.date
WHERE c.date < (SELECT as_of FROM params);
