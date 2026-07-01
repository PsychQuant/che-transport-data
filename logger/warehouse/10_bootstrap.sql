-- Bootstrap native DuckDB facts from the canonical Parquet lake.
--
-- Placeholders are rendered by run_warehouse_sql.py:
--   ${PARQUET_ROOT}        e.g. /Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet
--   ${DATE_FILTER}         SQL predicate over hive partition column "date"
--   ${FACT_DELETE_FILTER}  SQL predicate over native service_date/city
--   ${CITY_FILTER}         SQL predicate over hive/native city
--   ${LOAD_LABEL}          audit label

SET TimeZone='Asia/Taipei';

BEGIN TRANSACTION;

CREATE OR REPLACE TEMP VIEW src_arrival_event AS
SELECT
    CAST(city AS VARCHAR) AS city,
    CAST(date AS DATE) AS service_date,
    CAST(plate AS VARCHAR) AS plate,
    CAST(route_uid AS VARCHAR) AS route_uid,
    CAST(direction AS INTEGER) AS direction,
    CAST(stop_uid AS VARCHAR) AS stop_uid,
    CAST(stop_sequence AS INTEGER) AS stop_sequence,
    CAST(event_type AS INTEGER) AS event_type,
    CAST(COALESCE(event_ts, gps_time) AS TIMESTAMPTZ) AS event_ts,
    CAST(gps_time AS TIMESTAMPTZ) AS gps_time,
    CAST(gps_lat AS DOUBLE) AS gps_lat,
    CAST(gps_lon AS DOUBLE) AS gps_lon,
    CAST(captured_at AS TIMESTAMPTZ) AS captured_at,
    CAST(source AS VARCHAR) AS source,
    filename
FROM read_parquet(
    '${PARQUET_ROOT}/arrival_event/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true,
    filename=true
)
WHERE ${DATE_FILTER}
  AND ${CITY_FILTER}
  AND route_uid IS NOT NULL
  AND stop_uid IS NOT NULL
  AND direction IS NOT NULL
  AND event_type IS NOT NULL
  AND COALESCE(event_ts, gps_time) IS NOT NULL
  AND captured_at IS NOT NULL;

CREATE OR REPLACE TEMP VIEW src_vehicle_position AS
SELECT
    CAST(city AS VARCHAR) AS city,
    CAST(date AS DATE) AS service_date,
    CAST(plate AS VARCHAR) AS plate,
    CAST(route_uid AS VARCHAR) AS route_uid,
    CAST(sub_route_uid AS VARCHAR) AS sub_route_uid,
    CAST(direction AS INTEGER) AS direction,
    CAST(gps_time AS TIMESTAMPTZ) AS gps_time,
    CAST(gps_lat AS DOUBLE) AS gps_lat,
    CAST(gps_lon AS DOUBLE) AS gps_lon,
    CAST(speed AS DOUBLE) AS speed,
    CAST(azimuth AS DOUBLE) AS azimuth,
    CAST(duty_status AS INTEGER) AS duty_status,
    CAST(bus_status AS INTEGER) AS bus_status,
    CAST(captured_at AS TIMESTAMPTZ) AS captured_at,
    CAST(source AS VARCHAR) AS source,
    filename
FROM read_parquet(
    '${PARQUET_ROOT}/vehicle_position/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true,
    filename=true
)
WHERE ${DATE_FILTER}
  AND ${CITY_FILTER}
  AND captured_at IS NOT NULL;

CREATE OR REPLACE TEMP VIEW src_eta_snapshot AS
SELECT
    CAST(city AS VARCHAR) AS city,
    CAST(date AS DATE) AS service_date,
    CAST(captured_at AS TIMESTAMPTZ) AS captured_at,
    CAST(route_uid AS VARCHAR) AS route_uid,
    CAST(direction AS INTEGER) AS direction,
    CAST(stop_uid AS VARCHAR) AS stop_uid,
    CAST(estimate_time_sec AS INTEGER) AS estimate_time_sec,
    CAST(stop_status AS INTEGER) AS stop_status,
    CAST(plate AS VARCHAR) AS plate,
    CAST(src_update_time AS TIMESTAMPTZ) AS src_update_time,
    CAST(source AS VARCHAR) AS source,
    filename
FROM read_parquet(
    '${PARQUET_ROOT}/eta_snapshot/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true,
    filename=true
)
WHERE ${DATE_FILTER}
  AND ${CITY_FILTER}
  AND route_uid IS NOT NULL
  AND stop_uid IS NOT NULL
  AND direction IS NOT NULL
  AND captured_at IS NOT NULL;

INSERT INTO bus_eta.dim_city (city, city_name_zh, city_name_en)
SELECT city, city_name_zh, city_name_en
FROM (
    SELECT 'Taipei' AS city, '臺北市' AS city_name_zh, 'Taipei' AS city_name_en
    UNION ALL
    SELECT 'NewTaipei' AS city, '新北市' AS city_name_zh, 'New Taipei' AS city_name_en
) c
WHERE NOT EXISTS (
    SELECT 1 FROM bus_eta.dim_city d WHERE d.city = c.city
);

-- Seed route stubs so facts can resolve to a current dimension row before TDX
-- route metadata is backfilled. The SCD2 pattern hydrates __stub__ rows in-place.
INSERT INTO bus_eta.dim_route (
    route_sk, city, route_uid, route_id, route_name_zh, route_name_en,
    departure_stop, destination_stop, operator_id, attr_hash,
    valid_from, valid_to, is_current
)
WITH route_keys AS (
    SELECT DISTINCT city, route_uid FROM src_arrival_event
    UNION
    SELECT DISTINCT city, route_uid FROM src_vehicle_position WHERE route_uid IS NOT NULL
    UNION
    SELECT DISTINCT city, route_uid FROM src_eta_snapshot
),
missing AS (
    SELECT k.*
    FROM route_keys k
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_route d
        WHERE d.city = k.city AND d.route_uid = k.route_uid AND d.is_current
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(route_sk), 0) FROM bus_eta.dim_route)
        + row_number() OVER (ORDER BY city, route_uid) AS route_sk,
        *
    FROM missing
)
SELECT
    route_sk, city, route_uid,
    NULL, NULL, NULL, NULL, NULL, NULL,
    '__stub__',
    TIMESTAMPTZ '2000-01-01 00:00:00+08:00',
    NULL,
    TRUE
FROM numbered;

INSERT INTO bus_eta.dim_stop (
    stop_sk, city, stop_uid, stop_id, stop_name_zh, stop_name_en,
    stop_lat, stop_lon, attr_hash, valid_from, valid_to, is_current
)
WITH stop_keys AS (
    SELECT DISTINCT city, stop_uid FROM src_arrival_event
    UNION
    SELECT DISTINCT city, stop_uid FROM src_eta_snapshot
),
missing AS (
    SELECT k.*
    FROM stop_keys k
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_stop d
        WHERE d.city = k.city AND d.stop_uid = k.stop_uid AND d.is_current
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(stop_sk), 0) FROM bus_eta.dim_stop)
        + row_number() OVER (ORDER BY city, stop_uid) AS stop_sk,
        *
    FROM missing
)
SELECT
    stop_sk, city, stop_uid,
    NULL, NULL, NULL, NULL, NULL,
    '__stub__',
    TIMESTAMPTZ '2000-01-01 00:00:00+08:00',
    NULL,
    TRUE
FROM numbered;

INSERT INTO bus_eta.dim_vehicle (
    vehicle_sk, plate, operator_id, vehicle_type, attr_hash,
    valid_from, valid_to, is_current
)
WITH vehicle_keys AS (
    SELECT DISTINCT plate FROM src_arrival_event WHERE plate IS NOT NULL
    UNION
    SELECT DISTINCT plate FROM src_vehicle_position WHERE plate IS NOT NULL
    UNION
    SELECT DISTINCT plate FROM src_eta_snapshot WHERE plate IS NOT NULL
),
missing AS (
    SELECT k.*
    FROM vehicle_keys k
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_vehicle d
        WHERE d.plate = k.plate AND d.is_current
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(vehicle_sk), 0) FROM bus_eta.dim_vehicle)
        + row_number() OVER (ORDER BY plate) AS vehicle_sk,
        *
    FROM missing
)
SELECT
    vehicle_sk, plate,
    NULL, NULL, '__stub__',
    TIMESTAMPTZ '2000-01-01 00:00:00+08:00',
    NULL,
    TRUE
FROM numbered;

INSERT INTO bus_eta.bridge_route_stop (
    route_stop_sk, city, route_uid, direction, stop_sequence, stop_uid,
    attr_hash, valid_from, valid_to, is_current
)
WITH route_stop_keys AS (
    SELECT DISTINCT city, route_uid, direction, stop_sequence, stop_uid
    FROM src_arrival_event
    WHERE stop_sequence IS NOT NULL
),
missing AS (
    SELECT k.*
    FROM route_stop_keys k
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.bridge_route_stop d
        WHERE d.city = k.city
          AND d.route_uid = k.route_uid
          AND d.direction = k.direction
          AND d.stop_sequence = k.stop_sequence
          AND d.is_current
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(route_stop_sk), 0) FROM bus_eta.bridge_route_stop)
        + row_number() OVER (ORDER BY city, route_uid, direction, stop_sequence) AS route_stop_sk,
        *
    FROM missing
)
SELECT
    route_stop_sk, city, route_uid, direction, stop_sequence, stop_uid,
    md5(concat_ws('|', city, route_uid, CAST(direction AS VARCHAR),
                  CAST(stop_sequence AS VARCHAR), stop_uid)),
    TIMESTAMPTZ '2000-01-01 00:00:00+08:00',
    NULL,
    TRUE
FROM numbered;

DELETE FROM bus_eta.fact_arrival_event WHERE ${FACT_DELETE_FILTER};
DELETE FROM bus_eta.fact_vehicle_position WHERE ${FACT_DELETE_FILTER};
DELETE FROM bus_eta.fact_eta_snapshot WHERE ${FACT_DELETE_FILTER};
DELETE FROM bus_eta.warehouse_partition_load WHERE ${FACT_DELETE_FILTER};

INSERT INTO bus_eta.fact_arrival_event (
    event_key, city, service_date, plate, route_uid, direction, stop_uid,
    stop_sequence, event_type, event_ts, gps_time, gps_lat, gps_lon,
    captured_at, source
)
SELECT
    md5(concat_ws('|',
        'arrival_event',
        city,
        COALESCE(plate, ''),
        route_uid,
        CAST(direction AS VARCHAR),
        stop_uid,
        CAST(event_type AS VARCHAR),
        CAST(event_ts AS VARCHAR)
    )) AS event_key,
    city, service_date, plate, route_uid, direction, stop_uid,
    stop_sequence, event_type, event_ts, gps_time, gps_lat, gps_lon,
    captured_at, source
FROM (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY city, COALESCE(plate, ''), route_uid, direction,
                         stop_uid, event_type, event_ts
            ORDER BY captured_at, filename
        ) AS rn
    FROM src_arrival_event
) x
WHERE rn = 1
ORDER BY city, service_date, captured_at;

INSERT INTO bus_eta.fact_vehicle_position (
    position_sk, city, service_date, plate, route_uid, sub_route_uid,
    direction, gps_time, gps_lat, gps_lon, speed, azimuth,
    duty_status, bus_status, captured_at, source
)
WITH numbered AS (
    SELECT
        (SELECT COALESCE(MAX(position_sk), 0) FROM bus_eta.fact_vehicle_position)
        + row_number() OVER (
            ORDER BY city, service_date, captured_at, COALESCE(plate, ''),
                     COALESCE(route_uid, ''), COALESCE(gps_time, captured_at),
                     filename
        ) AS position_sk,
        *
    FROM src_vehicle_position
)
SELECT
    position_sk, city, service_date, plate, route_uid, sub_route_uid,
    direction, gps_time, gps_lat, gps_lon, speed, azimuth,
    duty_status, bus_status, captured_at, source
FROM numbered
ORDER BY city, service_date, captured_at;

INSERT INTO bus_eta.fact_eta_snapshot (
    snapshot_key, city, service_date, captured_at, route_uid, direction,
    stop_uid, estimate_time_sec, stop_status, plate, src_update_time, source
)
SELECT
    md5(concat_ws('|',
        'eta_snapshot',
        city,
        CAST(captured_at AS VARCHAR),
        route_uid,
        CAST(direction AS VARCHAR),
        stop_uid,
        COALESCE(plate, '')
    )) AS snapshot_key,
    city, service_date, captured_at, route_uid, direction,
    stop_uid, estimate_time_sec, stop_status, plate, src_update_time, source
FROM (
    SELECT
        *,
        row_number() OVER (
            PARTITION BY city, captured_at, route_uid, direction, stop_uid,
                         COALESCE(plate, '')
            ORDER BY filename
        ) AS rn
    FROM src_eta_snapshot
) x
WHERE rn = 1
ORDER BY city, service_date, captured_at;

INSERT INTO bus_eta.warehouse_partition_load (
    table_name, city, service_date, loaded_at, source_file_count,
    source_row_count, loaded_row_count, source_max_captured_at, load_label
)
SELECT
    src.table_name,
    src.city,
    src.service_date,
    current_timestamp,
    src.source_file_count,
    src.source_row_count,
    COALESCE(dst.loaded_row_count, 0) AS loaded_row_count,
    src.source_max_captured_at,
    '${LOAD_LABEL}'
FROM (
    SELECT 'arrival_event' AS table_name, city, service_date,
           COUNT(DISTINCT filename)::UBIGINT AS source_file_count,
           COUNT(*)::UBIGINT AS source_row_count,
           MAX(captured_at) AS source_max_captured_at
    FROM src_arrival_event
    GROUP BY city, service_date
    UNION ALL
    SELECT 'vehicle_position' AS table_name, city, service_date,
           COUNT(DISTINCT filename)::UBIGINT AS source_file_count,
           COUNT(*)::UBIGINT AS source_row_count,
           MAX(captured_at) AS source_max_captured_at
    FROM src_vehicle_position
    GROUP BY city, service_date
    UNION ALL
    SELECT 'eta_snapshot' AS table_name, city, service_date,
           COUNT(DISTINCT filename)::UBIGINT AS source_file_count,
           COUNT(*)::UBIGINT AS source_row_count,
           MAX(captured_at) AS source_max_captured_at
    FROM src_eta_snapshot
    GROUP BY city, service_date
) src
LEFT JOIN (
    SELECT 'arrival_event' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_row_count
    FROM bus_eta.fact_arrival_event
    WHERE ${FACT_DELETE_FILTER}
    GROUP BY city, service_date
    UNION ALL
    SELECT 'vehicle_position' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_row_count
    FROM bus_eta.fact_vehicle_position
    WHERE ${FACT_DELETE_FILTER}
    GROUP BY city, service_date
    UNION ALL
    SELECT 'eta_snapshot' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_row_count
    FROM bus_eta.fact_eta_snapshot
    WHERE ${FACT_DELETE_FILTER}
    GROUP BY city, service_date
) dst
  ON dst.table_name = src.table_name
 AND dst.city = src.city
 AND dst.service_date = src.service_date;

COMMIT;
