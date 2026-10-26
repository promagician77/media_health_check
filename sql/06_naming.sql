-- check: naming
-- Convention:  <client>_<platform>_<objective>_<yyyymm>_<audience>_<geo>
--   e.g. alder_meta_con_202609_lal1_us
-- Lower-case, underscore-separated, no spaces. Campaign names are the join key between the
-- ad platforms and GA4 (utm_campaign), so a hand-typed name silently breaks attribution joins.
WITH names AS (
  SELECT DISTINCT client_id, platform AS source, campaign_name FROM stg_spend
), flagged AS (
  SELECT *,
         NOT regexp_matches(campaign_name,
           '^' || client_id || '_' || source || '_(con|trf|awr|lead|app)_20[0-9]{4}_[a-z0-9]+_[a-z]{2}$') AS is_bad
  FROM names
)
SELECT client_id, source,
       COUNT(*)                                      AS campaigns,
       COUNT(*) FILTER (WHERE is_bad)                AS off_convention,
       string_agg(campaign_name, ' | ') FILTER (WHERE is_bad) AS examples
FROM flagged GROUP BY client_id, source
HAVING COUNT(*) FILTER (WHERE is_bad) > 0
ORDER BY client_id, source;
