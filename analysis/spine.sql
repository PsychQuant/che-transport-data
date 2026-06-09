-- analysis/spine.sql — aligned wide-table marts over the bus-eta raw thin-facts.
-- Engine: DuckDB. Run once to define views/macros, then query.
--   python:  con=duckdb.connect(); con.execute(open('analysis/spine.sql').read())
--   (mini has no duckdb CLI; install pytz in the venv for timestamptz output.)
--
-- DATA_ROOT below = mini canonical NVMe path. ← edit the 3 literals if you
-- rsync the Parquet to the laptop. read_parquet needs a constant path.

-- ── source views over raw Parquet ───────────────────────────────────────────
CREATE OR REPLACE VIEW a1 AS  -- A1 vehicle_position: GPS positions, ~10s, full stream
  SELECT * FROM read_parquet('/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet/vehicle_position/**/*.parquet', hive_partitioning=true, union_by_name=true);
CREATE OR REPLACE VIEW a2 AS  -- A2 arrival_event: stop-touch events (deduped truth)
  SELECT * FROM read_parquet('/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet/arrival_event/**/*.parquet', hive_partitioning=true, union_by_name=true);
CREATE OR REPLACE VIEW n1 AS  -- N1 eta_snapshot: ETA predictions, ~120s
  SELECT * FROM read_parquet('/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet/eta_snapshot/**/*.parquet', hive_partitioning=true, union_by_name=true);

-- A2 event_type: 1 = 到站 (arrival), 0 = 離站 (departure)  [dedup.py + verified counts]
CREATE OR REPLACE VIEW arrivals AS
  SELECT plate, route_uid, direction, stop_uid, stop_sequence, gps_time AS actual_arrival, city
  FROM a2 WHERE event_type = 1 AND plate IS NOT NULL;

-- ── Mart A: vehicle trajectory (grain: plate × time grid) ────────────────────
-- ASOF carry-forward: each grid tick gets the bus's last-known A1 position.
-- Bounded window — a full-history × all-plates grid would be billions of rows.
-- Usage: SELECT * FROM trajectory(TIMESTAMPTZ '2026-06-09 14:00+08:00',
--                                 TIMESTAMPTZ '2026-06-09 15:00+08:00', 10);
CREATE OR REPLACE MACRO trajectory(t0, t1, step_sec) AS TABLE (
  WITH grid AS (
    SELECT p.plate, g.ts
    FROM (SELECT DISTINCT plate FROM a1 WHERE plate IS NOT NULL) p
    CROSS JOIN (SELECT UNNEST(generate_series(t0, t1, to_seconds(step_sec))) AS ts) g
  )
  SELECT grid.plate, grid.ts,
         a1.gps_lat, a1.gps_lon, a1.speed, a1.azimuth, a1.route_uid,
         a1.gps_time AS pos_time,
         date_diff('second', a1.gps_time, grid.ts) AS pos_staleness_sec
  FROM grid
  ASOF LEFT JOIN a1
    ON grid.plate = a1.plate AND grid.ts >= a1.gps_time
);

-- ── Mart B: prediction error (grain: each actual arrival) ─────────────────────
-- The north star: for every real arrival (A2), the last N1 prediction made
-- at-or-before it, and the error. ASOF on n1.captured_at <= actual_arrival.
CREATE OR REPLACE VIEW prediction_error AS
  SELECT
    ar.plate, ar.route_uid, ar.direction, ar.stop_uid, ar.city,
    ar.actual_arrival,
    n1.captured_at                                              AS pred_observed_at,
    n1.src_update_time                                          AS pred_src_time,
    n1.estimate_time_sec,
    n1.captured_at + (n1.estimate_time_sec * INTERVAL 1 SECOND) AS predicted_arrival,
    date_diff('second',
              n1.captured_at + (n1.estimate_time_sec * INTERVAL 1 SECOND),
              ar.actual_arrival)                                AS error_sec,      -- +ve = arrived later than predicted
    date_diff('second', n1.captured_at, ar.actual_arrival)      AS lead_time_sec   -- how far ahead the prediction was made
  -- N1 carries NO plate (it's a per-route/stop ETA, plate is 100%% null in the
  -- feed), so match on (route, direction, stop) only; ar.plate (output) is the
  -- bus that fulfilled it. Filter to real predictions (estimate not null; ~32%%
  -- of N1 rows are 未發車/過站 placeholders). Caveat: on short headways two buses
  -- to one stop are ambiguous — ASOF takes the latest prediction before arrival.
  FROM arrivals ar
  ASOF LEFT JOIN (SELECT * FROM n1 WHERE estimate_time_sec IS NOT NULL) n1
    ON ar.route_uid = n1.route_uid AND ar.direction = n1.direction
       AND ar.stop_uid = n1.stop_uid
       AND ar.actual_arrival >= n1.captured_at;

-- ── example queries ───────────────────────────────────────────────────────────
-- 1. One bus's trajectory (14:00–15:00, 10s grid):
--    SELECT * FROM trajectory(TIMESTAMPTZ '2026-06-09 14:00+08:00',
--                             TIMESTAMPTZ '2026-06-09 15:00+08:00', 10)
--    WHERE plate='030-FV' ORDER BY ts;
-- 2. TDX ETA error distribution (the baseline to beat):
--    SELECT count(*) n, median(abs(error_sec)) med_abs_err,
--           quantile_cont(abs(error_sec),0.9) p90_abs_err
--    FROM prediction_error WHERE error_sec IS NOT NULL;
-- 3. Error by how-far-ahead the prediction was:
--    SELECT (lead_time_sec/60)::INT AS lead_min, count(*) n, median(error_sec) med_err
--    FROM prediction_error WHERE error_sec IS NOT NULL AND lead_time_sec BETWEEN 0 AND 1800
--    GROUP BY 1 ORDER BY 1;
