-- check: duplicates
-- Grain is (client, platform, day, campaign). Anything above one row means a re-run or
-- a missing de-dupe, and every sum downstream is inflated.
WITH spend_dups AS (
  SELECT client_id, platform AS source, date, campaign_id, COUNT(*) AS n
  FROM stg_spend GROUP BY client_id, platform, date, campaign_id HAVING COUNT(*) > 1
), ga4_dups AS (
  SELECT client_id, 'ga4' AS source, event_date AS date,
         session_source || '/' || session_medium || '/' || session_campaign AS campaign_id, COUNT(*) AS n
  FROM raw_ga4_traffic
  GROUP BY client_id, event_date, session_source, session_medium, session_campaign HAVING COUNT(*) > 1
), dups AS (SELECT * FROM spend_dups UNION ALL SELECT * FROM ga4_dups)
SELECT client_id, source, COUNT(*) AS duplicated_keys, SUM(n - 1) AS extra_rows,
       COUNT(DISTINCT date) AS days_affected, MIN(date) AS first_date, MAX(date) AS last_date
FROM dups GROUP BY client_id, source
ORDER BY client_id, source;
