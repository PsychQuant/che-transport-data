"""Unit tests for fetch_tdx_staging pure mapping functions (no network/DB)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "warehouse"))
from fetch_tdx_staging import (  # noqa: E402
    map_route, map_stop, map_vehicle, map_route_stops,
)


def test_map_route_full_record():
    rec = {
        "RouteUID": "TPE10132", "RouteID": "10132",
        "RouteName": {"Zh_tw": "232", "En": "232"},
        "DepartureStopNameZh": "捷運昆陽站", "DestinationStopNameZh": "青年公園",
        "Operators": [{"OperatorID": "100"}, {"OperatorID": "200"}],
    }
    assert map_route(rec, "Taipei") == (
        "Taipei", "TPE10132", "10132", "232", "232",
        "捷運昆陽站", "青年公園", "100")


def test_map_route_missing_optionals():
    assert map_route({"RouteUID": "X"}, "Taipei") == (
        "Taipei", "X", None, None, None, None, None, None)


def test_map_stop():
    rec = {
        "StopUID": "TPE1001", "StopID": "1001",
        "StopName": {"Zh_tw": "台北車站", "En": "Taipei Main Sta."},
        "StopPosition": {"PositionLat": 25.047, "PositionLon": 121.517},
    }
    assert map_stop(rec, "Taipei") == (
        "Taipei", "TPE1001", "1001", "台北車站", "Taipei Main Sta.",
        25.047, 121.517)


def test_map_vehicle_type_stringified():
    assert map_vehicle({"PlateNumb": "KKA-1234", "OperatorID": "100",
                        "VehicleType": 1}) == ("KKA-1234", "100", "1")
    assert map_vehicle({"PlateNumb": "KKA-1"}) == ("KKA-1", None, None)


def test_map_route_stops_expands_and_skips_incomplete():
    rec = {
        "RouteUID": "NWT10164", "Direction": 0,
        "Stops": [
            {"StopUID": "NWT218125", "StopSequence": 38},
            {"StopUID": None, "StopSequence": 39},        # skipped
            {"StopUID": "NWT218764", "StopSequence": 38},  # sub-route variant
        ],
    }
    assert map_route_stops(rec, "NewTaipei") == [
        ("NewTaipei", "NWT10164", 0, 38, "NWT218125"),
        ("NewTaipei", "NWT10164", 0, 38, "NWT218764"),
    ]


def test_map_route_stops_missing_direction_yields_nothing():
    assert map_route_stops({"RouteUID": "X", "Stops": [
        {"StopUID": "S", "StopSequence": 1}]}, "Taipei") == []


def test_route_stop_dedup_across_subroute_entries():
    a = map_route_stops({"RouteUID": "R", "Direction": 1, "Stops": [
        {"StopUID": "S1", "StopSequence": 1}]}, "Taipei")
    b = map_route_stops({"RouteUID": "R", "Direction": 1, "Stops": [
        {"StopUID": "S1", "StopSequence": 1}]}, "Taipei")
    assert len(set(a) | set(b)) == 1
