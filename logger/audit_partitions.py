#!/usr/bin/env python3
"""External partition audit for the bus-eta parquet lake.

Runs as its own daily launchd job, deliberately outside the poller. The
2026-07-31..08-02 hole survived twelve days because the only gap detector lived
inside the very process that had stopped producing data — a detector that needs
its subject to be healthy cannot report that its subject is not.

Checks two things per feed/city, from the filesystem alone:
  - missing days   : a calendar date with no partition inside the collected span
  - low-volume days: a partition whose file count collapsed versus the median

Exits 1 when anything is found, and appends a JSON line to <root>/../audit/ so
the finding survives log rotation.

NOTE: the launchd plist MUST invoke this with the FDA-granted python directly.
A /bin/sh wrapper makes /bin/sh the TCC responsible process, which has no
Removable Volumes grant — that is what silently killed the warehouse job for
41 days (see issue #3). Same volume, same trap.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import statistics
import sys

import notify

TPE = datetime.timezone(datetime.timedelta(hours=8))
FEEDS = ("arrival_event", "eta_snapshot", "vehicle_position")
JOB = "bus-eta-audit"  # == launchd label == notify state key


def audit_dates(counts: dict, *, skip_dates=(), min_ratio: float = 0.5) -> dict:
    """Find missing and low-volume days in {iso_date: file_count}.

    `skip_dates` (typically today's partial partition) is excluded both from the
    verdict and from the median — a short partial day must never lower the bar
    far enough for a real outage to slip through.
    """
    skip = set(skip_dates)
    judged = {d: n for d, n in counts.items() if d not in skip}

    missing: list = []
    if counts:
        days = sorted(datetime.date.fromisoformat(d) for d in counts)
        present = set(counts)
        day = days[0]
        while day <= days[-1]:
            if day.isoformat() not in present:
                missing.append(day.isoformat())
            day += datetime.timedelta(days=1)

    low: list = []
    if len(judged) >= 2:
        median = statistics.median(judged.values())
        threshold = median * min_ratio
        low = sorted(d for d, n in judged.items() if n < threshold)

    return {"missing": missing, "low_volume": low}


def recent(counts: dict, *, today: str, window_days: int) -> dict:
    """Keep only the last `window_days` days (inclusive of both ends); 0 = no limit.

    Old holes are permanent and already documented. Re-reporting them every night
    turns the audit into noise, and a job that always says ANOMALY is a job nobody
    reads — which is precisely how the 41-day warehouse outage stayed invisible
    (#3). The audit only earns attention if a finding means something new.
    """
    if window_days <= 0:
        return dict(counts)
    oldest = datetime.date.fromisoformat(today) - datetime.timedelta(days=window_days - 1)
    return {d: n for d, n in counts.items() if datetime.date.fromisoformat(d) >= oldest}


def has_anomalies(result: dict) -> bool:
    return bool(result["missing"] or result["low_volume"])


# ── filesystem scan ────────────────────────────────────────────────────────
def count_files_by_date(feed_city_dir: str) -> dict:
    """{iso_date: parquet file count} for one <feed>/city=<X>/ directory."""
    counts = {}
    base = pathlib.Path(feed_city_dir)
    if not base.is_dir():
        return counts
    for part in base.iterdir():
        if not part.is_dir() or not part.name.startswith("date="):
            continue
        counts[part.name.split("=", 1)[1]] = sum(
            1 for f in part.iterdir() if f.suffix == ".parquet"
        )
    return counts


def scan(parquet_root: str, cities, today: str, min_ratio: float,
         window_days: int) -> list:
    findings = []
    for feed in FEEDS:
        for city in cities:
            d = os.path.join(parquet_root, feed, f"city={city}")
            counts = recent(count_files_by_date(d), today=today, window_days=window_days)
            if not counts:
                findings.append({"feed": feed, "city": city, "error": "no partitions found"})
                continue
            result = audit_dates(counts, skip_dates=(today,), min_ratio=min_ratio)
            if has_anomalies(result):
                findings.append({"feed": feed, "city": city, **result})
    return findings


def main(argv: list) -> int:
    """Run the audit and report the outcome (issue #5).

    Catches BaseException, not Exception: the mount guard below signals an
    absent NVMe with `raise SystemExit`, which does NOT inherit from Exception.
    `except Exception` here would silently drop the single most important alert
    this job can produce.
    """
    sink, state_path = notify.from_env()
    try:
        rc = _run(argv)
    except BaseException as exc:
        notify.report(JOB, ok=False, detail=notify.summarize(exc),
                      sink=sink, state_path=state_path)
        raise
    # A finding IS the failure worth telling someone about: the program ran
    # fine, the data has a hole.
    notify.report(JOB, ok=(rc == 0), detail="" if rc == 0 else "partition audit found anomalies",
                  sink=sink, state_path=state_path)
    return rc


def _run(argv: list) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parquet-root", required=True)
    p.add_argument("--cities", default="Taipei,NewTaipei")
    p.add_argument("--min-ratio", type=float, default=0.5,
                   help="flag a day whose file count is below this × the median")
    p.add_argument("--window-days", type=int, default=14,
                   help="only audit the last N days (0 = full history). Keeps the "
                        "daily job silent unless something NEW broke; permanent "
                        "historical holes are documented, not re-alerted nightly")
    p.add_argument("--report-dir", help="defaults to <parquet-root>/../audit")
    args = p.parse_args(argv)

    if not pathlib.Path(args.parquet_root).is_dir():
        raise SystemExit(
            f"volume not mounted (no such directory: {args.parquet_root}); "
            "cannot audit — treating as failure rather than reporting all-clear"
        )

    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    now = datetime.datetime.now(TPE)
    today = now.date().isoformat()
    findings = scan(args.parquet_root, cities, today, args.min_ratio, args.window_days)

    report = {"audited_at": now.isoformat(), "today_skipped": today,
              "min_ratio": args.min_ratio, "window_days": args.window_days,
              "findings": findings}

    report_dir = args.report_dir or os.path.normpath(
        os.path.join(args.parquet_root, "..", "audit"))
    os.makedirs(report_dir, exist_ok=True)
    with open(os.path.join(report_dir, "partition_audit.jsonl"), "a") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")

    if not findings:
        print(f"{today}\tOK\tno missing or low-volume partitions")
        return 0
    for item in findings:
        print(f"{today}\tANOMALY\t{json.dumps(item, ensure_ascii=False)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
