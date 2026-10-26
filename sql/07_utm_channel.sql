-- check: utm_channel
-- Take every campaign we PAY for (it exists in an ad-platform spend table), find its
-- sessions in GA4, and ask which channel GA4 filed them under. A paid Meta campaign tagged
-- utm_medium=social falls out of 'Paid Social' and into 'Organic Social': the UTM is present,
-- so a "UTM exists" QA step passes, and the paid number on the dashboard is short.
WITH paid AS (
  SELECT DISTINCT client_id, platform, campaign_name,
         CASE platform WHEN 'meta' THEN 'Paid Social' WHEN 'gads' THEN 'Paid Search' ELSE 'Display' END AS expected_channel
  FROM stg_spend
), joined AS (
  SELECT g.client_id, p.campaign_name, p.platform, p.expected_channel, g.channel AS actual_channel,
         g.event_date, g.sessions
  FROM stg_ga4_channel g
  JOIN paid p ON p.client_id = g.client_id AND p.campaign_name = g.session_campaign
), per_campaign AS (
  SELECT client_id, campaign_name, platform, expected_channel,
         SUM(sessions)                                              AS total_sessions,
         SUM(sessions) FILTER (WHERE actual_channel <> expected_channel) AS misfiled_sessions,
         MIN(event_date) FILTER (WHERE actual_channel <> expected_channel) AS first_date,
         MAX(event_date) FILTER (WHERE actual_channel <> expected_channel) AS last_date,
         max(actual_channel) FILTER (WHERE actual_channel <> expected_channel) AS filed_as
  FROM joined GROUP BY client_id, campaign_name, platform, expected_channel
)
SELECT client_id, 'ga4' AS source, campaign_name, expected_channel, filed_as,
       misfiled_sessions, total_sessions, misfiled_sessions * 1.0 / total_sessions AS misfiled_share,
       first_date, last_date
FROM per_campaign
WHERE misfiled_sessions * 1.0 / total_sessions >= 0.02
ORDER BY client_id, misfiled_sessions DESC;
