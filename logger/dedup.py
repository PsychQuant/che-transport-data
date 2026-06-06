"""A2 arrival-event deduplication.

TDX A2 (RealTimeNearStop) re-reports the same vehicle near the same stop every
poll while it is present. Collapse a run of reports for the same
(plate, route_uid, direction, stop_uid, event_type) into a single arrival event,
keeping the earliest GPSTime. A gap > window_sec between consecutive reports
starts a new event (the vehicle left and returned on a later trip).

The dedup key is a superset of the spec's stated (plate, stop_uid, direction):
route_uid + event_type are added so a plate switching routes — or arrive(1) vs
depart(0) — are never merged. This matches fact_arrival_event's primary key.
"""
from collections import defaultdict

DEFAULT_WINDOW_SEC = 90


def _key(r):
    return (r["plate"], r["route_uid"], r["direction"], r["stop_uid"], r["event_type"])


def _event_from_cluster(cluster):
    first = min(cluster, key=lambda r: r["gps_time"])  # earliest GPSTime wins
    e = dict(first)
    e["event_ts"] = first["gps_time"]
    return e


def dedup_arrival_events(reports, window_sec=DEFAULT_WINDOW_SEC):
    """Collapse A2 reports into arrival events. Pure function; input order-independent."""
    groups = defaultdict(list)
    for r in reports:
        groups[_key(r)].append(r)

    events = []
    for rs in groups.values():
        rs = sorted(rs, key=lambda r: r["gps_time"])
        cluster = [rs[0]]
        for prev, cur in zip(rs, rs[1:]):
            if (cur["gps_time"] - prev["gps_time"]).total_seconds() <= window_sec:
                cluster.append(cur)
            else:
                events.append(_event_from_cluster(cluster))
                cluster = [cur]
        events.append(_event_from_cluster(cluster))
    return events
