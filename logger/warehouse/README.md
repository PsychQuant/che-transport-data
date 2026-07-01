# Bus ETA DuckDB Warehouse

This directory builds a persistent DuckDB database beside the canonical Parquet
lake on mini-che.

Recommended location on mini:

```bash
DB=/Volumes/mini-2TB-SSD/che-transport/bus-eta/warehouse/bus_eta.duckdb
PARQUET=/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet
PY=/path/to/bus-eta-logger/.venv/bin/python
```

## Model Choice

The canonical lake stays as append-only Hive-partitioned Parquet. The DuckDB
file is a portable analytical snapshot:

- native facts: copied into `.duckdb` for single-file rsync and repeated-query speed
- dimensions: native SCD Type-2 tables
- resolved wide tables: views only, not canonical storage

Facts stay thin: no route names, stop names, or coordinates are repeated in fact
rows. Wide analytical shape is produced by `v_arrival_event_resolved` and
`v_eta_snapshot_resolved`.

This is not strict Kimball star storage. It is a normalized core with star-like
views. That keeps BCNF properties for mutable metadata while preserving OLAP
ergonomics.

## Bootstrap

Full load:

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode bootstrap \
  --db "$DB" \
  --parquet-root "$PARQUET"
```

One date or bounded range, useful for testing:

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode bootstrap \
  --db "$DB" \
  --parquet-root "$PARQUET" \
  --load-date 2026-06-30
```

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode bootstrap \
  --db "$DB" \
  --parquet-root "$PARQUET" \
  --start-date 2026-06-20 \
  --end-date 2026-06-30
```

## Incremental Load

Load by date partition. The script replaces the target date/city partitions in a
transaction, then inserts the current Parquet rows. Rerunning the same date is
idempotent.

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode incremental \
  --db "$DB" \
  --parquet-root "$PARQUET" \
  --load-date 2026-06-30
```

Operational rule:

- load yesterday as the complete daily partition
- optionally reload today repeatedly for near-current analysis
- do not use row-level append watermarks as the only guard; logger writes files
  continuously, and partition replacement is simpler to reason about

## Verify

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode verify \
  --db "$DB" \
  --parquet-root "$PARQUET" \
  --load-date 2026-06-30
```

The verification compares source Parquet rows to loaded native rows for
`arrival_event`, `vehicle_position`, and `eta_snapshot`, and checks that fact
tables do not contain route/stop name columns.

## DuckDB Syntax Probe

Run this on mini to confirm the actual installed DuckDB build:

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode compat \
  --db /tmp/bus_eta_compat.duckdb \
  --parquet-root "$PARQUET"
```

The operational load avoids `MERGE INTO` and `INSERT ... ON CONFLICT`; the probe
tests them only so future scripts can rely on verified behavior.

## TDX Dimension Backfill

Populate staging tables from TDX static APIs:

- `bus_eta.stg_tdx_route_current` from Bus Route API
- `bus_eta.stg_tdx_stop_current` from Bus Stop API
- `bus_eta.stg_tdx_route_stop_current` from StopOfRoute API
- `bus_eta.stg_tdx_vehicle_current` from a future vehicle source, if available

Then run:

```bash
$PY logger/warehouse/run_warehouse_sql.py \
  --mode scd2 \
  --db "$DB" \
  --parquet-root "$PARQUET" \
  --scd-valid-from "2026-07-01 00:00:00+08:00"
```

`__stub__` rows created from facts are hydrated in-place. Later attribute
changes create new SCD2 versions with `valid_from`, `valid_to`, and
`is_current`.
