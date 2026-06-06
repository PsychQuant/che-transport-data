"""Parquet storage for bus-eta-logger.

Writes thin-fact rows as Hive-partitioned Parquet (city=<code>/date=<YYYY-MM-DD>/)
under the canonical root on the mini-che external NVMe. Refuses to write when the
external volume is not mounted — and never falls back to the system disk.
"""
import os
import uuid

import pyarrow as pa
import pyarrow.dataset as ds


class ExternalVolumeNotMounted(Exception):
    """Raised when the external NVMe is not mounted. Writing is refused; the
    system/boot disk is never used as a fallback."""


def volume_is_mounted(volume_path: str) -> bool:
    """True iff volume_path is an actual mount point (a mounted external volume),
    not just a directory living on the boot disk."""
    return os.path.ismount(volume_path)


def write_events(events, data_root, table_name, *, mounted, ts_field="event_ts"):
    """Write events as Hive-partitioned Parquet under data_root/table_name.

    `mounted` is the policy gate (caller computes it via volume_is_mounted on the
    external volume root). When False, refuse — write nothing, raise.
    Partition columns: city (from each record) + date (derived from ts_field).
    Returns the number of rows written.
    """
    if not mounted:
        raise ExternalVolumeNotMounted(
            f"external volume for {data_root!r} is not mounted; refusing to write "
            f"(will NOT fall back to system disk)"
        )
    if not events:
        return 0

    rows = []
    for e in events:
        r = dict(e)
        r["date"] = e[ts_field].date().isoformat()  # YYYY-MM-DD partition
        rows.append(r)

    table = pa.Table.from_pylist(rows)
    base = os.path.join(data_root, table_name)
    part = ds.partitioning(
        pa.schema([("city", pa.string()), ("date", pa.string())]), flavor="hive"
    )
    ds.write_dataset(
        table,
        base_dir=base,
        format="parquet",
        partitioning=part,
        existing_data_behavior="overwrite_or_ignore",
        # unique per write → never overwrite a prior cycle's files
        basename_template=uuid.uuid4().hex + "-{i}.parquet",
    )
    return len(rows)
