from datetime import datetime
import dedup

def R(plate, stop, d, t, route="R1", et=1):
    ts = datetime.fromisoformat(t)
    return {"plate": plate, "route_uid": route, "direction": d, "stop_uid": stop,
            "event_type": et, "gps_time": ts, "gps_lat": 25.0, "gps_lon": 121.5,
            "captured_at": ts, "source": "A2"}

def test_spec_example_three_reports_one_event_earliest_gpstime():
    reports = [R("EAL-5200","S123",0,"2026-03-10T09:24:01"),
               R("EAL-5200","S123",0,"2026-03-10T09:24:31"),
               R("EAL-5200","S123",0,"2026-03-10T09:24:58")]
    events = dedup.dedup_arrival_events(reports)
    assert len(events) == 1
    assert events[0]["event_ts"] == datetime.fromisoformat("2026-03-10T09:24:01")

def test_gap_over_window_splits_into_two_events():
    reports = [R("P1","S1",0,"2026-03-10T09:00:00"),
               R("P1","S1",0,"2026-03-10T09:30:00")]
    assert len(dedup.dedup_arrival_events(reports)) == 2

def test_boundary_exactly_90s_same_event():
    reports = [R("P1","S1",0,"2026-03-10T09:00:00"),
               R("P1","S1",0,"2026-03-10T09:01:30")]  # exactly 90s gap → still same
    assert len(dedup.dedup_arrival_events(reports)) == 1

def test_different_plate_not_merged():
    reports = [R("P1","S1",0,"2026-03-10T09:00:00"),
               R("P2","S1",0,"2026-03-10T09:00:10")]
    assert len(dedup.dedup_arrival_events(reports)) == 2

def test_different_event_type_not_merged():
    reports = [R("P1","S1",0,"2026-03-10T09:00:00", et=1),
               R("P1","S1",0,"2026-03-10T09:00:10", et=0)]
    assert len(dedup.dedup_arrival_events(reports)) == 2

def test_earliest_kept_regardless_of_input_order():
    reports = [R("P1","S1",0,"2026-03-10T09:00:30"),
               R("P1","S1",0,"2026-03-10T09:00:01"),
               R("P1","S1",0,"2026-03-10T09:00:20")]
    events = dedup.dedup_arrival_events(reports)
    assert len(events) == 1
    assert events[0]["event_ts"] == datetime.fromisoformat("2026-03-10T09:00:01")

def test_empty_input():
    assert dedup.dedup_arrival_events([]) == []
