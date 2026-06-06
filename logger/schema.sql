-- bus-eta-logger storage schema (BCNF + SCD Type-2)
-- Facts are thin (natural FK + measures + timestamps, NO descriptive names).
-- Dimensions: route/stop/vehicle/bridge are SCD-2 (surrogate sk + validity window);
-- city/operator are Type-1.

-- ── Type-1 dimensions ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dim_city (
    city_code     VARCHAR PRIMARY KEY,
    name          VARCHAR
);

CREATE TABLE IF NOT EXISTS dim_operator (
    operator_id   VARCHAR PRIMARY KEY,
    operator_name VARCHAR
);

-- ── SCD Type-2 dimensions ───────────────────────────────────────────
-- surrogate *_sk = PK; (natural_key, valid_from) = alternate candidate key (UNIQUE);
-- valid_to NULL = open/current interval; is_current mirrors that for fast filter.
CREATE TABLE IF NOT EXISTS dim_route (
    route_sk    BIGINT      PRIMARY KEY,
    route_uid   VARCHAR     NOT NULL,
    city_code   VARCHAR     NOT NULL,
    route_id    VARCHAR,
    name_zh     VARCHAR,
    name_en     VARCHAR,
    dep_stop    VARCHAR,
    dest_stop   VARCHAR,
    operator_id VARCHAR,
    valid_from  TIMESTAMP   NOT NULL,
    valid_to    TIMESTAMP,
    is_current  BOOLEAN     NOT NULL,
    UNIQUE (route_uid, valid_from)
);

CREATE TABLE IF NOT EXISTS dim_stop (
    stop_sk     BIGINT      PRIMARY KEY,
    stop_uid    VARCHAR     NOT NULL,
    city_code   VARCHAR     NOT NULL,
    stop_id     VARCHAR,
    name_zh     VARCHAR,
    name_en     VARCHAR,
    lat         DOUBLE,
    lon         DOUBLE,
    valid_from  TIMESTAMP   NOT NULL,
    valid_to    TIMESTAMP,
    is_current  BOOLEAN     NOT NULL,
    UNIQUE (stop_uid, valid_from)
);

CREATE TABLE IF NOT EXISTS dim_vehicle (
    vehicle_sk   BIGINT     PRIMARY KEY,
    plate        VARCHAR    NOT NULL,
    vehicle_type VARCHAR,
    operator_id  VARCHAR,
    valid_from   TIMESTAMP  NOT NULL,
    valid_to     TIMESTAMP,
    is_current   BOOLEAN    NOT NULL,
    UNIQUE (plate, valid_from)
);

CREATE TABLE IF NOT EXISTS bridge_route_stop (
    rs_sk         BIGINT    PRIMARY KEY,
    route_uid     VARCHAR   NOT NULL,
    direction     INTEGER   NOT NULL,
    stop_sequence INTEGER   NOT NULL,
    stop_uid      VARCHAR   NOT NULL,
    valid_from    TIMESTAMP NOT NULL,
    valid_to      TIMESTAMP,
    is_current    BOOLEAN   NOT NULL,
    UNIQUE (route_uid, direction, stop_sequence, valid_from)
);

-- ── Thin facts (natural FK + measures + timestamps; no names) ────────
-- A2 arrival/departure events (after dedup). event_type: 0=depart, 1=arrive.
CREATE TABLE IF NOT EXISTS fact_arrival_event (
    plate       VARCHAR   NOT NULL,
    route_uid   VARCHAR   NOT NULL,
    direction   INTEGER   NOT NULL,
    stop_uid    VARCHAR   NOT NULL,
    event_type  INTEGER   NOT NULL CHECK (event_type IN (0, 1)),
    event_ts    TIMESTAMP NOT NULL,
    gps_lat     DOUBLE,
    gps_lon     DOUBLE,
    captured_at TIMESTAMP NOT NULL,
    source      VARCHAR   NOT NULL,
    PRIMARY KEY (plate, route_uid, direction, stop_uid, event_type, event_ts)
);

-- N1 ETA baseline snapshots.
CREATE TABLE IF NOT EXISTS fact_eta_snapshot (
    captured_at       TIMESTAMP NOT NULL,
    route_uid         VARCHAR   NOT NULL,
    direction         INTEGER   NOT NULL,
    stop_uid          VARCHAR   NOT NULL,
    estimate_time_sec INTEGER,
    stop_status       INTEGER,
    plate             VARCHAR,
    source            VARCHAR   NOT NULL,
    PRIMARY KEY (captured_at, route_uid, direction, stop_uid)
);

-- ── As-of join view: resolve each arrival event to the dim_stop version
--    current at event_ts. LEFT JOIN so events are never dropped when a dim
--    version is missing (audit: confused/lazy-dev would INNER JOIN + lose rows).
CREATE VIEW IF NOT EXISTS v_arrival_event_resolved AS
SELECT
    e.*,
    s.stop_sk      AS stop_sk,
    s.name_zh      AS stop_name_zh,
    s.name_en      AS stop_name_en,
    s.lat          AS stop_lat,
    s.lon          AS stop_lon
FROM fact_arrival_event e
LEFT JOIN dim_stop s
  ON  s.stop_uid = e.stop_uid
  AND e.event_ts >= s.valid_from
  AND (s.valid_to IS NULL OR e.event_ts < s.valid_to);
