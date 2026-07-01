-- Persistent DuckDB warehouse schema for bus ETA logs.
-- Target: DuckDB 1.4+. Facts are thin and native for portable single-file
-- analysis; Parquet remains the canonical append-only source.

SET TimeZone='Asia/Taipei';

CREATE SCHEMA IF NOT EXISTS bus_eta;

-- Type-1 dimensions.
CREATE TABLE IF NOT EXISTS bus_eta.dim_city (
    city             VARCHAR PRIMARY KEY,
    city_name_zh     VARCHAR,
    city_name_en     VARCHAR
);

CREATE TABLE IF NOT EXISTS bus_eta.dim_operator (
    operator_id      VARCHAR PRIMARY KEY,
    operator_name_zh VARCHAR,
    operator_name_en VARCHAR,
    updated_at       TIMESTAMPTZ
);

-- SCD Type-2 dimensions. The *_sk columns are physical primary keys; the
-- natural key plus valid_from is the historical candidate key.
CREATE TABLE IF NOT EXISTS bus_eta.dim_route (
    route_sk         UBIGINT PRIMARY KEY,
    city             VARCHAR NOT NULL,
    route_uid        VARCHAR NOT NULL,
    route_id         VARCHAR,
    route_name_zh    VARCHAR,
    route_name_en    VARCHAR,
    departure_stop   VARCHAR,
    destination_stop VARCHAR,
    operator_id      VARCHAR,
    attr_hash        VARCHAR NOT NULL,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    is_current       BOOLEAN NOT NULL,
    UNIQUE (city, route_uid, valid_from)
);

CREATE TABLE IF NOT EXISTS bus_eta.dim_stop (
    stop_sk          UBIGINT PRIMARY KEY,
    city             VARCHAR NOT NULL,
    stop_uid         VARCHAR NOT NULL,
    stop_id          VARCHAR,
    stop_name_zh     VARCHAR,
    stop_name_en     VARCHAR,
    stop_lat         DOUBLE,
    stop_lon         DOUBLE,
    attr_hash        VARCHAR NOT NULL,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    is_current       BOOLEAN NOT NULL,
    UNIQUE (city, stop_uid, valid_from)
);

CREATE TABLE IF NOT EXISTS bus_eta.dim_vehicle (
    vehicle_sk       UBIGINT PRIMARY KEY,
    plate            VARCHAR NOT NULL,
    operator_id      VARCHAR,
    vehicle_type     VARCHAR,
    attr_hash        VARCHAR NOT NULL,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    is_current       BOOLEAN NOT NULL,
    UNIQUE (plate, valid_from)
);

CREATE TABLE IF NOT EXISTS bus_eta.bridge_route_stop (
    route_stop_sk    UBIGINT PRIMARY KEY,
    city             VARCHAR NOT NULL,
    route_uid        VARCHAR NOT NULL,
    direction        INTEGER NOT NULL,
    stop_sequence    INTEGER NOT NULL,
    stop_uid         VARCHAR NOT NULL,
    attr_hash        VARCHAR NOT NULL,
    valid_from       TIMESTAMPTZ NOT NULL,
    valid_to         TIMESTAMPTZ,
    is_current       BOOLEAN NOT NULL,
    UNIQUE (city, route_uid, direction, stop_sequence, valid_from)
);

-- Native thin facts copied from Parquet. Descriptive names stay in dimensions.
CREATE TABLE IF NOT EXISTS bus_eta.fact_arrival_event (
    event_key        VARCHAR PRIMARY KEY,
    city             VARCHAR NOT NULL,
    service_date     DATE NOT NULL,
    plate            VARCHAR,
    route_uid        VARCHAR NOT NULL,
    direction        INTEGER NOT NULL,
    stop_uid         VARCHAR NOT NULL,
    stop_sequence    INTEGER,
    event_type       INTEGER NOT NULL CHECK (event_type IN (0, 1)),
    event_ts         TIMESTAMPTZ NOT NULL,
    gps_time         TIMESTAMPTZ,
    gps_lat          DOUBLE,
    gps_lon          DOUBLE,
    captured_at      TIMESTAMPTZ NOT NULL,
    source           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS bus_eta.fact_vehicle_position (
    position_sk      UBIGINT PRIMARY KEY,
    city             VARCHAR NOT NULL,
    service_date     DATE NOT NULL,
    plate            VARCHAR,
    route_uid        VARCHAR,
    sub_route_uid    VARCHAR,
    direction        INTEGER,
    gps_time         TIMESTAMPTZ,
    gps_lat          DOUBLE,
    gps_lon          DOUBLE,
    speed            DOUBLE,
    azimuth          DOUBLE,
    duty_status      INTEGER,
    bus_status       INTEGER,
    captured_at      TIMESTAMPTZ NOT NULL,
    source           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS bus_eta.fact_eta_snapshot (
    snapshot_key      VARCHAR PRIMARY KEY,
    city              VARCHAR NOT NULL,
    service_date      DATE NOT NULL,
    captured_at       TIMESTAMPTZ NOT NULL,
    route_uid         VARCHAR NOT NULL,
    direction         INTEGER NOT NULL,
    stop_uid          VARCHAR NOT NULL,
    estimate_time_sec INTEGER,
    stop_status       INTEGER,
    plate             VARCHAR,
    src_update_time   TIMESTAMPTZ,
    source            VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS bus_eta.warehouse_partition_load (
    table_name           VARCHAR NOT NULL,
    city                 VARCHAR NOT NULL,
    service_date         DATE NOT NULL,
    loaded_at            TIMESTAMPTZ NOT NULL,
    source_file_count    UBIGINT,
    source_row_count     UBIGINT NOT NULL,
    loaded_row_count     UBIGINT NOT NULL,
    source_max_captured_at TIMESTAMPTZ,
    load_label           VARCHAR NOT NULL,
    PRIMARY KEY (table_name, city, service_date)
);

-- Analysis-friendly resolved views. These are intentionally denormalized views,
-- not canonical storage.
CREATE OR REPLACE VIEW bus_eta.v_arrival_event_resolved AS
SELECT
    e.*,
    r.route_sk,
    r.route_name_zh,
    r.route_name_en,
    s.stop_sk,
    s.stop_name_zh,
    s.stop_name_en,
    s.stop_lat,
    s.stop_lon,
    v.vehicle_sk
FROM bus_eta.fact_arrival_event e
LEFT JOIN bus_eta.dim_route r
  ON r.city = e.city
 AND r.route_uid = e.route_uid
 AND e.event_ts >= r.valid_from
 AND (r.valid_to IS NULL OR e.event_ts < r.valid_to)
LEFT JOIN bus_eta.dim_stop s
  ON s.city = e.city
 AND s.stop_uid = e.stop_uid
 AND e.event_ts >= s.valid_from
 AND (s.valid_to IS NULL OR e.event_ts < s.valid_to)
LEFT JOIN bus_eta.dim_vehicle v
  ON v.plate = e.plate
 AND e.event_ts >= v.valid_from
 AND (v.valid_to IS NULL OR e.event_ts < v.valid_to);

CREATE OR REPLACE VIEW bus_eta.v_eta_snapshot_resolved AS
SELECT
    n.*,
    r.route_sk,
    r.route_name_zh,
    r.route_name_en,
    s.stop_sk,
    s.stop_name_zh,
    s.stop_name_en,
    s.stop_lat,
    s.stop_lon
FROM bus_eta.fact_eta_snapshot n
LEFT JOIN bus_eta.dim_route r
  ON r.city = n.city
 AND r.route_uid = n.route_uid
 AND n.captured_at >= r.valid_from
 AND (r.valid_to IS NULL OR n.captured_at < r.valid_to)
LEFT JOIN bus_eta.dim_stop s
  ON s.city = n.city
 AND s.stop_uid = n.stop_uid
 AND n.captured_at >= s.valid_from
 AND (s.valid_to IS NULL OR n.captured_at < s.valid_to);
