"""Resident poll daemon for bus-eta-logger.

Wires the pieces together: Cadence decides which feeds are due → TDXClient fetches
per-city bulk → A2 is deduped to arrival events, N1 stored as ETA snapshots →
storage writes Hive-partitioned Parquet on the external NVMe (refusing if not
mounted). run_cycle does ONE cycle (unit-tested); main() is the thin loop.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import dedup
import metrics
import storage
from tdx_client import Cadence, TDXClient

TPE = timezone(timedelta(hours=8))


@dataclass
class Config:
    cities: list = field(default_factory=lambda: ["Taipei", "NewTaipei"])
    intervals: dict = field(default_factory=lambda: {"A2": 30, "N1": 120})
    volume_path: str = "/Volumes/CHANGE_ME"          # mount-check root (NVMe)
    data_root: str = "/Volumes/CHANGE_ME/che-transport/bus-eta/parquet"
    state_file: str = os.path.expanduser("~/.bus-eta-logger/heartbeat.txt")
    tick_sec: int = 5
    token_refresh_sec: int = 3000


def new_state():
    return {"dedup_total": 0, "cycles_with_data": 0, "mount_errors": 0, "skips": 0}


def run_cycle(client, cadence, cfg, state, now):
    """Run one poll cycle: fetch due feeds, dedup A2, write Parquet. Never crashes
    on a not-mounted volume — records the error and skips."""
    due = cadence.due(now)
    if not due:
        return
    if not storage.volume_is_mounted(cfg.volume_path):
        state["mount_errors"] += 1
        return  # refuse to write; do NOT fall back to system disk; retry next tick
    wrote_any = False
    for feed in due:
        for city in cfg.cities:
            recs = client.fetch_bulk(city, feed)
            if recs is None:          # rate-limited twice → skip this feed/city
                state["skips"] += 1
                continue
            if feed == "A2":
                events = dedup.dedup_arrival_events(recs)
                n = storage.write_events(events, cfg.data_root, "arrival_event", mounted=True)
                state["dedup_total"] += metrics.dedup_count(len(recs), len(events))
            else:
                n = storage.write_events(recs, cfg.data_root, "eta_snapshot",
                                         mounted=True, ts_field="captured_at")
            wrote_any = wrote_any or n > 0
        cadence.mark(feed, now)
    if wrote_any:
        state["cycles_with_data"] += 1


# ── daemon plumbing (not unit-tested; exercised by the capture-feasibility run) ──
def _keychain(account):
    return subprocess.run(
        ["security", "find-generic-password", "-s", "che-transport-tdx", "-a", account, "-w"],
        capture_output=True, text=True,
    ).stdout.strip()


def _read_heartbeat(path):
    try:
        return datetime.fromisoformat(open(path).read().strip())
    except (OSError, ValueError):
        return None


def _write_heartbeat(path, now):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(now.isoformat())


def _record_gap(cfg, gap):
    d = os.path.join(cfg.data_root, "..", "gaps")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gaps.jsonl"), "a") as f:
        f.write(json.dumps({k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in gap.items()}) + "\n")


def main():
    cfg = Config(
        volume_path=os.environ.get("BUS_ETA_VOLUME", Config.volume_path),
        data_root=os.environ.get("BUS_ETA_DATA_ROOT", Config.data_root),
    )
    client = TDXClient(_keychain("client_id"), _keychain("client_secret"))
    client.get_token()
    token_t = time.monotonic()

    # gap-on-restart: compare last heartbeat against now
    now = datetime.now(TPE)
    last = _read_heartbeat(cfg.state_file)
    gap = metrics.detect_gap(last, now, min(cfg.intervals.values()))
    if gap and storage.volume_is_mounted(cfg.volume_path):
        _record_gap(cfg, gap)

    cadence = Cadence(cfg.intervals)
    state = new_state()
    while True:
        now = datetime.now(TPE)
        if time.monotonic() - token_t > cfg.token_refresh_sec:
            client.get_token(); token_t = time.monotonic()
        run_cycle(client, cadence, cfg, state, now)
        if storage.volume_is_mounted(cfg.volume_path):
            _write_heartbeat(cfg.state_file, now)
        time.sleep(cfg.tick_sec)


if __name__ == "__main__":
    main()
