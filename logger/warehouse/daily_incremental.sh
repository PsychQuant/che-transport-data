#!/bin/sh
# Daily warehouse load: yesterday's complete partition (idempotent partition-replace).
# Driven by launchd tw.psychquant.bus-eta-warehouse (StartCalendarInterval 03:30).
set -eu

VOL="${BUS_ETA_VOLUME:-/Volumes/mini-2TB-SSD}"
ROOT="$VOL/che-transport/bus-eta"

# mount guard — never touch the system disk if the NVMe is absent
if [ ! -d "$ROOT/parquet" ]; then
    echo "$(date '+%F %T') volume not mounted ($ROOT); aborting" >&2
    exit 1
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
YESTERDAY="$(date -v-1d +%F)"

exec "$REPO/logger/.venv/bin/python" "$REPO/logger/warehouse/run_warehouse_sql.py" \
    --mode incremental \
    --db "$ROOT/warehouse/warehouse.duckdb" \
    --parquet-root "$ROOT/parquet" \
    --load-date "$YESTERDAY"
