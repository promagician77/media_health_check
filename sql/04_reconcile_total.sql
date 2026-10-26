-- check: reconcile_total
-- Warehouse spend vs the platform's own control total, day by day. Tolerance 1%.
-- Platforms revise spend slightly after the fact, so exact equality would be a false alarm.
SELECT client_id, platform AS source,
       COUNT(*) FILTER (WHERE abs(err_pct) > 0.01)             AS days_off,
       COUNT(*)                                                 AS days_checked,
       SUM(err)                                                 AS net_err,
       SUM(abs(err))                                            AS gross_err,
       SUM(control_spend)                                       AS control_total,
       max_by(date, abs(err_pct))                               AS worst_day,
       max(abs(err_pct))                                        AS worst_err_pct,
       max_by(warehouse_spend / NULLIF(control_spend, 0), abs(err_pct)) AS worst_ratio
FROM recon_daily
GROUP BY client_id, platform
HAVING COUNT(*) FILTER (WHERE abs(err_pct) > 0.01) > 0
ORDER BY client_id, source;
