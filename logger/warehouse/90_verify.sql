-- Verification for a rendered date/city slice.
--
-- Placeholders:
--   ${PARQUET_ROOT}
--   ${DATE_FILTER}
--   ${FACT_DELETE_FILTER}
--   ${CITY_FILTER}

SET TimeZone='Asia/Taipei';

CREATE OR REPLACE TEMP VIEW verify_source_counts AS
SELECT 'arrival_event' AS table_name, city, CAST(date AS DATE) AS service_date, COUNT(*)::UBIGINT AS source_rows
FROM read_parquet(
    '${PARQUET_ROOT}/arrival_event/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true
)
WHERE ${DATE_FILTER} AND ${CITY_FILTER}
GROUP BY city, CAST(date AS DATE)
UNION ALL
SELECT 'vehicle_position' AS table_name, city, CAST(date AS DATE) AS service_date, COUNT(*)::UBIGINT AS source_rows
FROM read_parquet(
    '${PARQUET_ROOT}/vehicle_position/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true
)
WHERE ${DATE_FILTER} AND ${CITY_FILTER}
GROUP BY city, CAST(date AS DATE)
UNION ALL
SELECT 'eta_snapshot' AS table_name, city, CAST(date AS DATE) AS service_date, COUNT(*)::UBIGINT AS source_rows
FROM read_parquet(
    '${PARQUET_ROOT}/eta_snapshot/**/*.parquet',
    hive_partitioning=true,
    union_by_name=true
)
WHERE ${DATE_FILTER} AND ${CITY_FILTER}
GROUP BY city, CAST(date AS DATE);

CREATE OR REPLACE TEMP VIEW verify_loaded_counts AS
SELECT 'arrival_event' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_rows
FROM bus_eta.fact_arrival_event
WHERE ${FACT_DELETE_FILTER}
GROUP BY city, service_date
UNION ALL
SELECT 'vehicle_position' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_rows
FROM bus_eta.fact_vehicle_position
WHERE ${FACT_DELETE_FILTER}
GROUP BY city, service_date
UNION ALL
SELECT 'eta_snapshot' AS table_name, city, service_date, COUNT(*)::UBIGINT AS loaded_rows
FROM bus_eta.fact_eta_snapshot
WHERE ${FACT_DELETE_FILTER}
GROUP BY city, service_date;

CREATE OR REPLACE TEMP VIEW verify_fact_name_columns AS
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'bus_eta'
  AND table_name IN ('fact_arrival_event', 'fact_vehicle_position', 'fact_eta_snapshot')
  AND regexp_matches(lower(column_name), '(route|stop).*name|name.*(route|stop)');

CREATE OR REPLACE TEMP VIEW verify_dim_current_duplicates AS
SELECT 'dim_route' AS table_name, city || '|' || route_uid AS natural_key, COUNT(*)::UBIGINT AS current_rows
FROM bus_eta.dim_route
WHERE is_current
GROUP BY city, route_uid
HAVING COUNT(*) > 1
UNION ALL
SELECT 'dim_stop' AS table_name, city || '|' || stop_uid AS natural_key, COUNT(*)::UBIGINT AS current_rows
FROM bus_eta.dim_stop
WHERE is_current
GROUP BY city, stop_uid
HAVING COUNT(*) > 1
UNION ALL
SELECT 'dim_vehicle' AS table_name, plate AS natural_key, COUNT(*)::UBIGINT AS current_rows
FROM bus_eta.dim_vehicle
WHERE is_current
GROUP BY plate
HAVING COUNT(*) > 1
UNION ALL
SELECT 'bridge_route_stop' AS table_name,
       city || '|' || route_uid || '|' || CAST(direction AS VARCHAR) || '|' || CAST(stop_sequence AS VARCHAR) AS natural_key,
       COUNT(*)::UBIGINT AS current_rows
FROM bus_eta.bridge_route_stop
WHERE is_current
GROUP BY city, route_uid, direction, stop_sequence
HAVING COUNT(*) > 1;

WITH count_check AS (
    SELECT
        COALESCE(s.table_name, l.table_name) AS table_name,
        COALESCE(s.city, l.city) AS city,
        COALESCE(s.service_date, l.service_date) AS service_date,
        COALESCE(s.source_rows, 0) AS source_rows,
        COALESCE(l.loaded_rows, 0) AS loaded_rows,
        COALESCE(s.source_rows, 0) = COALESCE(l.loaded_rows, 0) AS ok
    FROM verify_source_counts s
    FULL OUTER JOIN verify_loaded_counts l
      ON l.table_name = s.table_name
     AND l.city = s.city
     AND l.service_date = s.service_date
),
schema_check AS (
    SELECT
        'fact_name_columns' AS table_name,
        COALESCE(table_name || '.' || column_name, '-') AS city,
        NULL::DATE AS service_date,
        0::UBIGINT AS source_rows,
        COUNT(*) OVER ()::UBIGINT AS loaded_rows,
        COUNT(*) OVER () = 0 AS ok
    FROM verify_fact_name_columns
),
dim_check AS (
    SELECT
        'dim_current_duplicates' AS table_name,
        COALESCE(table_name || ':' || natural_key, '-') AS city,
        NULL::DATE AS service_date,
        0::UBIGINT AS source_rows,
        COUNT(*) OVER ()::UBIGINT AS loaded_rows,
        COUNT(*) OVER () = 0 AS ok
    FROM verify_dim_current_duplicates
)
SELECT * FROM count_check
UNION ALL
SELECT * FROM schema_check
UNION ALL
SELECT * FROM dim_check
ORDER BY table_name, city, service_date;
