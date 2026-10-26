-- check: ga4_headroom
-- Early warning, not a post-mortem. Standard GA4 properties have a daily BigQuery export limit
-- of 1,000,000 events; a property that consistently exceeds it has the daily export paused,
-- and previous days' exports are not reprocessed. The notification email goes to property
-- editors and administrators, which is often not the person who owns the dashboard.
-- Alert when the 7-day average of collected events reaches 85% of the limit.
WITH roll AS (
  SELECT client_id, event_date, events_collected, events_exported,
         AVG(events_collected) OVER (PARTITION BY client_id ORDER BY event_date
                                     ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS avg7
  FROM raw_ga4_export_log
), lim AS (SELECT ga4_event_limit AS lim FROM params)
SELECT r.client_id, 'ga4' AS source,
       MIN(r.event_date) AS first_breach_date,
       MAX(r.avg7)       AS peak_avg7,
       MAX(r.events_collected) AS peak_day,
       (SELECT lim FROM lim) AS export_limit,
       MIN(r.event_date) FILTER (WHERE r.events_exported IS NULL) AS first_missing_export_date
FROM roll r
WHERE r.avg7 >= 0.85 * (SELECT lim FROM lim)
GROUP BY r.client_id
ORDER BY r.client_id;
