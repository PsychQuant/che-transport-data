-- SCD Type-2 maintenance patterns for TDX skeleton backfill.
--
-- Feed these staging tables from TDX static/skeleton APIs:
--   Route API        -> stg_tdx_route_current
--   Stop API         -> stg_tdx_stop_current
--   StopOfRoute API  -> stg_tdx_route_stop_current
--
-- The loader creates __stub__ dimension rows from facts. The first TDX backfill
-- hydrates those stubs in-place because filling previously unknown attributes is
-- a correction, not a real historical change. Later differences close the prior
-- current row and insert a new SCD2 version.
--
-- Placeholder rendered by run_warehouse_sql.py:
--   ${SCD_VALID_FROM_EXPR}  e.g. current_timestamp or TIMESTAMPTZ '2026-07-01 00:00:00+08:00'

SET TimeZone='Asia/Taipei';

CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_route_current (
    city             VARCHAR NOT NULL,
    route_uid        VARCHAR NOT NULL,
    route_id         VARCHAR,
    route_name_zh    VARCHAR,
    route_name_en    VARCHAR,
    departure_stop   VARCHAR,
    destination_stop VARCHAR,
    operator_id      VARCHAR
);

CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_stop_current (
    city             VARCHAR NOT NULL,
    stop_uid         VARCHAR NOT NULL,
    stop_id          VARCHAR,
    stop_name_zh     VARCHAR,
    stop_name_en     VARCHAR,
    stop_lat         DOUBLE,
    stop_lon         DOUBLE
);

CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_vehicle_current (
    plate            VARCHAR NOT NULL,
    operator_id      VARCHAR,
    vehicle_type     VARCHAR
);

CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_route_stop_current (
    city             VARCHAR NOT NULL,
    route_uid        VARCHAR NOT NULL,
    direction        INTEGER NOT NULL,
    stop_sequence    INTEGER NOT NULL,
    stop_uid         VARCHAR NOT NULL
);

BEGIN TRANSACTION;

CREATE OR REPLACE TEMP VIEW stg_route_hashed AS
SELECT
    city,
    route_uid,
    route_id,
    route_name_zh,
    route_name_en,
    departure_stop,
    destination_stop,
    operator_id,
    md5(concat_ws('|',
        COALESCE(city, ''),
        COALESCE(route_uid, ''),
        COALESCE(route_id, ''),
        COALESCE(route_name_zh, ''),
        COALESCE(route_name_en, ''),
        COALESCE(departure_stop, ''),
        COALESCE(destination_stop, ''),
        COALESCE(operator_id, '')
    )) AS attr_hash,
    ${SCD_VALID_FROM_EXPR} AS valid_from
FROM bus_eta.stg_tdx_route_current
WHERE city IS NOT NULL AND route_uid IS NOT NULL;

UPDATE bus_eta.dim_route d
SET route_id = s.route_id,
    route_name_zh = s.route_name_zh,
    route_name_en = s.route_name_en,
    departure_stop = s.departure_stop,
    destination_stop = s.destination_stop,
    operator_id = s.operator_id,
    attr_hash = s.attr_hash
FROM stg_route_hashed s
WHERE d.city = s.city
  AND d.route_uid = s.route_uid
  AND d.is_current
  AND d.attr_hash = '__stub__';

UPDATE bus_eta.dim_route d
SET valid_to = s.valid_from,
    is_current = FALSE
FROM stg_route_hashed s
WHERE d.city = s.city
  AND d.route_uid = s.route_uid
  AND d.is_current
  AND d.attr_hash <> s.attr_hash
  AND d.attr_hash <> '__stub__';

INSERT INTO bus_eta.dim_route (
    route_sk, city, route_uid, route_id, route_name_zh, route_name_en,
    departure_stop, destination_stop, operator_id, attr_hash,
    valid_from, valid_to, is_current
)
WITH missing_current AS (
    SELECT s.*
    FROM stg_route_hashed s
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_route d
        WHERE d.city = s.city
          AND d.route_uid = s.route_uid
          AND d.is_current
          AND d.attr_hash = s.attr_hash
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(route_sk), 0) FROM bus_eta.dim_route)
        + row_number() OVER (ORDER BY city, route_uid) AS route_sk,
        *
    FROM missing_current
)
SELECT
    route_sk, city, route_uid, route_id, route_name_zh, route_name_en,
    departure_stop, destination_stop, operator_id, attr_hash,
    valid_from, NULL, TRUE
FROM numbered;

CREATE OR REPLACE TEMP VIEW stg_stop_hashed AS
SELECT
    city,
    stop_uid,
    stop_id,
    stop_name_zh,
    stop_name_en,
    stop_lat,
    stop_lon,
    md5(concat_ws('|',
        COALESCE(city, ''),
        COALESCE(stop_uid, ''),
        COALESCE(stop_id, ''),
        COALESCE(stop_name_zh, ''),
        COALESCE(stop_name_en, ''),
        COALESCE(CAST(stop_lat AS VARCHAR), ''),
        COALESCE(CAST(stop_lon AS VARCHAR), '')
    )) AS attr_hash,
    ${SCD_VALID_FROM_EXPR} AS valid_from
FROM bus_eta.stg_tdx_stop_current
WHERE city IS NOT NULL AND stop_uid IS NOT NULL;

UPDATE bus_eta.dim_stop d
SET stop_id = s.stop_id,
    stop_name_zh = s.stop_name_zh,
    stop_name_en = s.stop_name_en,
    stop_lat = s.stop_lat,
    stop_lon = s.stop_lon,
    attr_hash = s.attr_hash
FROM stg_stop_hashed s
WHERE d.city = s.city
  AND d.stop_uid = s.stop_uid
  AND d.is_current
  AND d.attr_hash = '__stub__';

UPDATE bus_eta.dim_stop d
SET valid_to = s.valid_from,
    is_current = FALSE
FROM stg_stop_hashed s
WHERE d.city = s.city
  AND d.stop_uid = s.stop_uid
  AND d.is_current
  AND d.attr_hash <> s.attr_hash
  AND d.attr_hash <> '__stub__';

INSERT INTO bus_eta.dim_stop (
    stop_sk, city, stop_uid, stop_id, stop_name_zh, stop_name_en,
    stop_lat, stop_lon, attr_hash, valid_from, valid_to, is_current
)
WITH missing_current AS (
    SELECT s.*
    FROM stg_stop_hashed s
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_stop d
        WHERE d.city = s.city
          AND d.stop_uid = s.stop_uid
          AND d.is_current
          AND d.attr_hash = s.attr_hash
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(stop_sk), 0) FROM bus_eta.dim_stop)
        + row_number() OVER (ORDER BY city, stop_uid) AS stop_sk,
        *
    FROM missing_current
)
SELECT
    stop_sk, city, stop_uid, stop_id, stop_name_zh, stop_name_en,
    stop_lat, stop_lon, attr_hash, valid_from, NULL, TRUE
FROM numbered;

CREATE OR REPLACE TEMP VIEW stg_vehicle_hashed AS
SELECT
    plate,
    operator_id,
    vehicle_type,
    md5(concat_ws('|',
        COALESCE(plate, ''),
        COALESCE(operator_id, ''),
        COALESCE(vehicle_type, '')
    )) AS attr_hash,
    ${SCD_VALID_FROM_EXPR} AS valid_from
FROM bus_eta.stg_tdx_vehicle_current
WHERE plate IS NOT NULL;

UPDATE bus_eta.dim_vehicle d
SET operator_id = s.operator_id,
    vehicle_type = s.vehicle_type,
    attr_hash = s.attr_hash
FROM stg_vehicle_hashed s
WHERE d.plate = s.plate
  AND d.is_current
  AND d.attr_hash = '__stub__';

UPDATE bus_eta.dim_vehicle d
SET valid_to = s.valid_from,
    is_current = FALSE
FROM stg_vehicle_hashed s
WHERE d.plate = s.plate
  AND d.is_current
  AND d.attr_hash <> s.attr_hash
  AND d.attr_hash <> '__stub__';

INSERT INTO bus_eta.dim_vehicle (
    vehicle_sk, plate, operator_id, vehicle_type, attr_hash,
    valid_from, valid_to, is_current
)
WITH missing_current AS (
    SELECT s.*
    FROM stg_vehicle_hashed s
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.dim_vehicle d
        WHERE d.plate = s.plate
          AND d.is_current
          AND d.attr_hash = s.attr_hash
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(vehicle_sk), 0) FROM bus_eta.dim_vehicle)
        + row_number() OVER (ORDER BY plate) AS vehicle_sk,
        *
    FROM missing_current
)
SELECT
    vehicle_sk, plate, operator_id, vehicle_type, attr_hash,
    valid_from, NULL, TRUE
FROM numbered;

CREATE OR REPLACE TEMP VIEW stg_route_stop_hashed AS
SELECT
    city,
    route_uid,
    direction,
    stop_sequence,
    stop_uid,
    md5(concat_ws('|',
        COALESCE(city, ''),
        COALESCE(route_uid, ''),
        COALESCE(CAST(direction AS VARCHAR), ''),
        COALESCE(CAST(stop_sequence AS VARCHAR), ''),
        COALESCE(stop_uid, '')
    )) AS attr_hash,
    ${SCD_VALID_FROM_EXPR} AS valid_from
FROM bus_eta.stg_tdx_route_stop_current
WHERE city IS NOT NULL
  AND route_uid IS NOT NULL
  AND direction IS NOT NULL
  AND stop_sequence IS NOT NULL
  AND stop_uid IS NOT NULL;

UPDATE bus_eta.bridge_route_stop d
SET stop_uid = s.stop_uid,
    attr_hash = s.attr_hash
FROM stg_route_stop_hashed s
WHERE d.city = s.city
  AND d.route_uid = s.route_uid
  AND d.direction = s.direction
  AND d.stop_sequence = s.stop_sequence
  AND d.is_current
  AND d.attr_hash = '__stub__';

UPDATE bus_eta.bridge_route_stop d
SET valid_to = s.valid_from,
    is_current = FALSE
FROM stg_route_stop_hashed s
WHERE d.city = s.city
  AND d.route_uid = s.route_uid
  AND d.direction = s.direction
  AND d.stop_sequence = s.stop_sequence
  AND d.is_current
  AND d.attr_hash <> s.attr_hash
  AND d.attr_hash <> '__stub__';

INSERT INTO bus_eta.bridge_route_stop (
    route_stop_sk, city, route_uid, direction, stop_sequence, stop_uid,
    attr_hash, valid_from, valid_to, is_current
)
WITH missing_current AS (
    SELECT s.*
    FROM stg_route_stop_hashed s
    WHERE NOT EXISTS (
        SELECT 1
        FROM bus_eta.bridge_route_stop d
        WHERE d.city = s.city
          AND d.route_uid = s.route_uid
          AND d.direction = s.direction
          AND d.stop_sequence = s.stop_sequence
          AND d.is_current
          AND d.attr_hash = s.attr_hash
    )
),
numbered AS (
    SELECT
        (SELECT COALESCE(MAX(route_stop_sk), 0) FROM bus_eta.bridge_route_stop)
        + row_number() OVER (ORDER BY city, route_uid, direction, stop_sequence) AS route_stop_sk,
        *
    FROM missing_current
)
SELECT
    route_stop_sk, city, route_uid, direction, stop_sequence, stop_uid,
    attr_hash, valid_from, NULL, TRUE
FROM numbered;

COMMIT;
