-- check: freshness
-- Is the newest loaded day yesterday? A pipeline that quietly stalls still serves a
-- dashboard; it just serves old numbers.
WITH latest AS (
  SELECT client_id, platform AS source, MAX(date) AS latest_date
  FROM stg_spend GROUP BY client_id, platform
  UNION ALL
  SELECT client_id, 'ga4', MAX(event_date)
  FROM raw_ga4_traffic GROUP BY client_id
), p AS (SELECT as_of - 1 AS expected_date FROM params)
SELECT l.client_id, l.source, l.latest_date, p.expected_date,
       date_diff('day', l.latest_date, p.expected_date) AS days_behind
FROM latest l CROSS JOIN p
WHERE l.latest_date < p.expected_date
ORDER BY l.client_id, l.source;
