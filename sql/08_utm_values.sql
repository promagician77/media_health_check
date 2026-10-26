-- check: utm_values
-- (a) spelling splits: the same source/medium written two ways. BigQuery string comparison is
--     case-sensitive, so a join or GROUP BY on the raw value treats Facebook and facebook as two
--     different sources.
-- (b) allow-list: a paid campaign whose medium is not the agreed one for its platform.
WITH splits AS (
  SELECT client_id, lower(session_source) AS norm_source, lower(session_medium) AS norm_medium,
         COUNT(DISTINCT session_source) AS source_spellings,
         COUNT(DISTINCT session_medium) AS medium_spellings,
         string_agg(DISTINCT session_source, ' | ') AS source_variants,
         SUM(sessions) AS sessions
  FROM raw_ga4_traffic
  GROUP BY client_id, lower(session_source), lower(session_medium)
  HAVING COUNT(DISTINCT session_source) > 1 OR COUNT(DISTINCT session_medium) > 1
), paid AS (
  SELECT DISTINCT client_id, platform, campaign_name,
         CASE platform WHEN 'meta' THEN 'paid_social' WHEN 'gads' THEN 'cpc' ELSE 'display' END AS agreed_medium
  FROM stg_spend
), off_list AS (
  SELECT g.client_id, p.campaign_name, g.session_medium, p.agreed_medium, SUM(g.sessions) AS sessions
  FROM raw_ga4_traffic g
  JOIN paid p ON p.client_id = g.client_id AND p.campaign_name = g.session_campaign
  WHERE lower(g.session_medium) <> p.agreed_medium
  GROUP BY g.client_id, p.campaign_name, g.session_medium, p.agreed_medium
)
SELECT client_id, 'ga4' AS source, 'spelling_split' AS kind,
       norm_source || '/' || norm_medium AS subject, source_variants AS detail, sessions
FROM splits
UNION ALL
SELECT client_id, 'ga4', 'off_allow_list', campaign_name,
       session_medium || ' (agreed: ' || agreed_medium || ')', sessions
FROM off_list
ORDER BY 1, 3;
