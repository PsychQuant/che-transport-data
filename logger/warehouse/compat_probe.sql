-- DuckDB SQL compatibility probe.
--
-- This is intentionally small and safe. Run it on mini before relying on syntax
-- claims for a specific DuckDB build:
--
--   python logger/warehouse/run_warehouse_sql.py \
--     --mode compat \
--     --db /tmp/bus_eta_compat.duckdb \
--     --parquet-root /Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet
--
-- Placeholder:
--   ${TMP_DIR}

SET TimeZone='Asia/Taipei';

CREATE TEMP TABLE compat_target(id INTEGER PRIMARY KEY, v VARCHAR);
INSERT INTO compat_target VALUES (1, 'a');

INSERT INTO compat_target VALUES (1, 'b')
ON CONFLICT(id) DO UPDATE SET v = excluded.v;

MERGE INTO compat_target AS t
USING (SELECT 1 AS id, 'c' AS v UNION ALL SELECT 2 AS id, 'd' AS v) AS s
ON t.id = s.id
WHEN MATCHED THEN UPDATE SET v = s.v
WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v);

COPY (
    SELECT
        'Taipei' AS city,
        DATE '2026-01-01' AS service_date,
        1 AS x
) TO '${TMP_DIR}/duckdb_compat_probe.parquet' (FORMAT parquet);

CREATE OR REPLACE TEMP VIEW compat_parquet AS
SELECT *
FROM read_parquet(
    '${TMP_DIR}/duckdb_compat_probe.parquet',
    union_by_name=true,
    filename=true
);

SELECT
    (SELECT string_agg(id || ':' || v, ',' ORDER BY id) FROM compat_target) AS merge_and_on_conflict_result,
    (SELECT COUNT(*) FROM compat_parquet) AS parquet_rows,
    (SELECT COUNT(*) FROM compat_parquet WHERE filename IS NOT NULL) AS filename_rows;
