-- check: day_boundary
-- Signature of a time-zone / day-boundary shift: many individual days are wrong, but the
-- period total roughly agrees and the signed errors cancel. Spend was MOVED between
-- days, not lost (stale or partial loads are all one sign) and not added (duplicates are
-- all the other sign).
WITH agg AS (
  SELECT client_id, platform AS source,
         COUNT(*) FILTER (WHERE abs(err_pct) > 0.01) AS days_off,
         COUNT(*)                                     AS days_checked,
         SUM(err)                                     AS net_err,
         SUM(abs(err))                                AS gross_err,
         SUM(control_spend)                           AS control_total
  FROM recon_daily GROUP BY client_id, platform
)
SELECT client_id, source, days_off, days_checked,
       net_err / control_total   AS period_err_pct,
       abs(net_err) / gross_err  AS net_to_gross
FROM agg
WHERE days_off >= 5
  AND abs(net_err) / control_total < 0.03
  AND abs(net_err) / gross_err < 0.25
ORDER BY client_id, source;
