-- analysis/spine.sql — aligned wide-table marts over the bus-eta raw thin-facts.
-- Engine: DuckDB. Run once to define views/macros, then query.
--   python:  con=duckdb.connect(); con.execute(open('analysis/spine.sql').read())
--   (mini has no duckdb CLI; install pytz in the venv for timestamptz output.)
--
-- DATA_ROOT below = mini canonical NVMe path. ← edit the 3 literals if you
-- rsync the Parquet to the laptop. read_parquet needs a constant path.

-- session tz so timestamptz -> date/hour extraction is Asia/Taipei
SET TimeZone='Asia/Taipei';

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

-- 台灣行政機關辦公日曆 (2026; is_holiday = 週末∪國定假日 − 補班日). Regen: analysis/tw_calendar.csv
CREATE OR REPLACE VIEW tw_holiday AS SELECT * FROM (VALUES (DATE '2026-01-01'), (DATE '2026-01-03'), (DATE '2026-01-04'), (DATE '2026-01-10'), (DATE '2026-01-11'), (DATE '2026-01-17'), (DATE '2026-01-18'), (DATE '2026-01-24'), (DATE '2026-01-25'), (DATE '2026-01-31'), (DATE '2026-02-01'), (DATE '2026-02-07'), (DATE '2026-02-08'), (DATE '2026-02-14'), (DATE '2026-02-15'), (DATE '2026-02-16'), (DATE '2026-02-17'), (DATE '2026-02-18'), (DATE '2026-02-19'), (DATE '2026-02-20'), (DATE '2026-02-21'), (DATE '2026-02-22'), (DATE '2026-02-27'), (DATE '2026-02-28'), (DATE '2026-03-01'), (DATE '2026-03-07'), (DATE '2026-03-08'), (DATE '2026-03-14'), (DATE '2026-03-15'), (DATE '2026-03-21'), (DATE '2026-03-22'), (DATE '2026-03-28'), (DATE '2026-03-29'), (DATE '2026-04-03'), (DATE '2026-04-04'), (DATE '2026-04-05'), (DATE '2026-04-06'), (DATE '2026-04-11'), (DATE '2026-04-12'), (DATE '2026-04-18'), (DATE '2026-04-19'), (DATE '2026-04-25'), (DATE '2026-04-26'), (DATE '2026-05-01'), (DATE '2026-05-02'), (DATE '2026-05-03'), (DATE '2026-05-09'), (DATE '2026-05-10'), (DATE '2026-05-16'), (DATE '2026-05-17'), (DATE '2026-05-23'), (DATE '2026-05-24'), (DATE '2026-05-30'), (DATE '2026-05-31'), (DATE '2026-06-06'), (DATE '2026-06-07'), (DATE '2026-06-13'), (DATE '2026-06-14'), (DATE '2026-06-19'), (DATE '2026-06-20'), (DATE '2026-06-21'), (DATE '2026-06-27'), (DATE '2026-06-28'), (DATE '2026-07-04'), (DATE '2026-07-05'), (DATE '2026-07-11'), (DATE '2026-07-12'), (DATE '2026-07-18'), (DATE '2026-07-19'), (DATE '2026-07-25'), (DATE '2026-07-26'), (DATE '2026-08-01'), (DATE '2026-08-02'), (DATE '2026-08-08'), (DATE '2026-08-09'), (DATE '2026-08-15'), (DATE '2026-08-16'), (DATE '2026-08-22'), (DATE '2026-08-23'), (DATE '2026-08-29'), (DATE '2026-08-30'), (DATE '2026-09-05'), (DATE '2026-09-06'), (DATE '2026-09-12'), (DATE '2026-09-13'), (DATE '2026-09-19'), (DATE '2026-09-20'), (DATE '2026-09-25'), (DATE '2026-09-26'), (DATE '2026-09-27'), (DATE '2026-09-28'), (DATE '2026-10-03'), (DATE '2026-10-04'), (DATE '2026-10-09'), (DATE '2026-10-10'), (DATE '2026-10-11'), (DATE '2026-10-17'), (DATE '2026-10-18'), (DATE '2026-10-24'), (DATE '2026-10-25'), (DATE '2026-10-26'), (DATE '2026-10-31'), (DATE '2026-11-01'), (DATE '2026-11-07'), (DATE '2026-11-08'), (DATE '2026-11-14'), (DATE '2026-11-15'), (DATE '2026-11-21'), (DATE '2026-11-22'), (DATE '2026-11-28'), (DATE '2026-11-29'), (DATE '2026-12-05'), (DATE '2026-12-06'), (DATE '2026-12-12'), (DATE '2026-12-13'), (DATE '2026-12-19'), (DATE '2026-12-20'), (DATE '2026-12-25'), (DATE '2026-12-26'), (DATE '2026-12-27')) t(d);

-- dwell time per stop: pair each arrival (event_type=1) with its departure (0).
-- ASOF to the first depart at-or-after the arrive at the same plate/route/dir/stop;
-- cap at 600s to drop cross-trip mispairs.
CREATE OR REPLACE VIEW dwell AS
  SELECT * FROM (
    SELECT a.plate, a.route_uid, a.direction, a.stop_uid, a.city,
           a.gps_time AS arrive_time, dp.gps_time AS depart_time,
           date_diff('second', a.gps_time, dp.gps_time) AS dwell_sec
    FROM (SELECT * FROM a2 WHERE event_type = 1 AND plate IS NOT NULL) a
    ASOF LEFT JOIN (SELECT * FROM a2 WHERE event_type = 0 AND plate IS NOT NULL) dp
      ON a.plate = dp.plate AND a.route_uid = dp.route_uid AND a.direction = dp.direction
         AND a.stop_uid = dp.stop_uid AND dp.gps_time >= a.gps_time
  ) WHERE dwell_sec IS NULL OR dwell_sec <= 600;

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
    date_diff('second', n1.captured_at, ar.actual_arrival)      AS lead_time_sec,  -- how far ahead the prediction was made
    hour(ar.actual_arrival)              AS arr_hour,
    isodow(ar.actual_arrival)            AS arr_dow,        -- 1=Mon .. 7=Sun
    isodow(ar.actual_arrival) IN (6, 7)  AS is_weekend,
    (ar.actual_arrival::DATE IN (SELECT d FROM tw_holiday)) AS is_holiday
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
