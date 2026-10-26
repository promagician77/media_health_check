-- check: definition
-- The metric registry says what each dashboard field reads. Two failure modes:
-- (a) overlapping action types. Meta returns one purchase under several action types
--     (purchase, omni_purchase, offsite_conversion.fb_pixel_purchase); adding them counts the
--     same purchase more than once. Impact is computed from the raw rows.
-- (b) a configured attribution window that never appears in the data. Meta removed 7d_view and
--     28d_view from the Insights API on 12 Jan 2026.
WITH reg AS (
  SELECT client_id, metric, platform, action_types, attribution_window,
         ',' || action_types || ',' AS padded
  FROM metric_registry WHERE platform = 'meta'
), overlap AS (
  SELECT client_id,
         (CASE WHEN padded LIKE '%,purchase,%' THEN 1 ELSE 0 END
        + CASE WHEN padded LIKE '%,omni_purchase,%' THEN 1 ELSE 0 END
        + CASE WHEN padded LIKE '%,offsite_conversion.fb_pixel_purchase,%' THEN 1 ELSE 0 END) AS overlapping_members,
         padded
  FROM reg
), impact AS (
  SELECT o.client_id, o.overlapping_members,
         SUM(a.value) FILTER (WHERE o.padded LIKE '%,' || a.action_type || ',%') AS dashboard_conversions,
         SUM(a.value) FILTER (WHERE a.action_type = 'purchase')                  AS canonical_conversions
  FROM overlap o
  JOIN (SELECT DISTINCT client_id, date_start, campaign_id, action_type, attribution_window, value
        FROM raw_meta_actions) a                       -- de-duplicated, so a double load can't skew the impact
    ON a.client_id = o.client_id
  WHERE o.overlapping_members > 1
  GROUP BY o.client_id, o.overlapping_members
)
SELECT client_id, 'meta' AS source, 'overlapping_action_types' AS kind,
       CAST(overlapping_members AS VARCHAR) AS detail,
       dashboard_conversions, canonical_conversions,
       dashboard_conversions * 1.0 / canonical_conversions AS inflation
FROM impact
UNION ALL
SELECT r.client_id, 'meta', 'window_not_in_data',
       r.attribution_window || CASE WHEN r.attribution_window IN ('7d_view', '28d_view')
            THEN ' (removed by Meta on 2026-01-12)' ELSE '' END,
       NULL, NULL, NULL
FROM reg r
WHERE NOT EXISTS (SELECT 1 FROM raw_meta_actions a
                  WHERE a.client_id = r.client_id AND a.attribution_window = r.attribution_window)
ORDER BY 1, 3;
