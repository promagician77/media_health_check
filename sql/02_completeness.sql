-- check: completeness
-- (a) interior gaps: days with no rows between the first and last loaded day
-- (b) partial loads: a day whose row count falls below 70% of the trailing-14-day average
WITH days AS (
  SELECT DISTINCT client_id, platform AS source, date FROM stg_spend
  UNION
  SELECT DISTINCT client_id, 'ga4', event_date FROM raw_ga4_traffic
), bounds AS (
  SELECT client_id, source, MIN(date) AS lo, MAX(date) AS hi FROM days GROUP BY client_id, source
), calendar AS (
  SELECT DISTINCT date FROM ctl_platform_totals          -- the platforms' own control totals define which days exist
), spine AS (
  SELECT b.client_id, b.source, c.date
  FROM bounds b JOIN calendar c ON c.date BETWEEN b.lo AND b.hi
), missing AS (
  SELECT s.client_id, s.source, s.date
  FROM spine s
  LEFT JOIN days d ON d.client_id = s.client_id AND d.source = s.source AND d.date = s.date
  WHERE d.date IS NULL
), rows_per_day AS (
  SELECT client_id, platform AS source, date, COUNT(*) AS n_rows
  FROM stg_spend GROUP BY client_id, platform, date
), scored AS (
  SELECT *, AVG(n_rows) OVER (PARTITION BY client_id, source ORDER BY date
                                 ROWS BETWEEN 14 PRECEDING AND 1 PRECEDING) AS trailing_avg
  FROM rows_per_day
)
SELECT client_id, source, 'missing_days' AS kind, COUNT(*) AS n,
       MIN(date) AS first_date, MAX(date) AS last_date, NULL AS n_rows, NULL AS trailing_avg
FROM missing GROUP BY client_id, source
UNION ALL
SELECT client_id, source, 'partial_load', 1, date, date, n_rows, trailing_avg
FROM scored
WHERE trailing_avg IS NOT NULL AND n_rows < 0.7 * trailing_avg
ORDER BY 1, 2, 3;
