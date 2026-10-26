-- check: tag_ratio
-- Post-launch QA as a number: GA4 sessions per ad-platform click, by campaign.
-- Healthy ratios are stable (Meta ~0.7, Search ~0.9, Display ~0.5 in this data).
-- A ratio that falls below half of its own first-two-weeks baseline means clicks are still
-- being paid for but the tag is not firing: the classic "new landing page shipped without GTM".
-- Only days where GA4 loaded for that client are compared, so a GA4 export gap is not
-- mistaken for a tag failure.
WITH ga4_days AS (SELECT DISTINCT client_id, event_date FROM raw_ga4_traffic),
clicks AS (
  SELECT s.client_id, s.platform, s.campaign_name, s.date, SUM(s.clicks) AS clicks
  FROM stg_spend s JOIN ga4_days d ON d.client_id = s.client_id AND d.event_date = s.date
  GROUP BY s.client_id, s.platform, s.campaign_name, s.date
), sess AS (
  SELECT client_id, session_campaign AS campaign_name, event_date AS date, SUM(sessions) AS sessions
  FROM raw_ga4_traffic GROUP BY client_id, session_campaign, event_date
), j AS (
  SELECT c.client_id, c.platform, c.campaign_name, c.date, c.clicks, COALESCE(s.sessions, 0) AS sessions
  FROM clicks c LEFT JOIN sess s
    ON s.client_id = c.client_id AND s.campaign_name = c.campaign_name AND s.date = c.date
), win AS (
  SELECT client_id, platform, campaign_name,
         SUM(sessions) FILTER (WHERE date < DATE '2026-10-12') * 1.0 / NULLIF(SUM(clicks) FILTER (WHERE date < DATE '2026-10-12'), 0) AS base_ratio,
         SUM(sessions) FILTER (WHERE date >= (SELECT as_of - 8 FROM params)) * 1.0 / NULLIF(SUM(clicks) FILTER (WHERE date >= (SELECT as_of - 8 FROM params)), 0) AS recent_ratio
  FROM j GROUP BY client_id, platform, campaign_name
)
SELECT client_id, platform AS source, campaign_name, base_ratio, recent_ratio,
       recent_ratio / base_ratio AS retained
FROM win
WHERE recent_ratio < 0.5 * base_ratio
ORDER BY client_id, source, campaign_name;
