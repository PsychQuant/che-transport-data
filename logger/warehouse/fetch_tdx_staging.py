#!/usr/bin/env python3
"""Fetch TDX static/skeleton APIs into the warehouse staging tables.

Populates (full refresh: DELETE + INSERT):
    bus_eta.stg_tdx_route_current       <- v2/Bus/Route/City/{city}
    bus_eta.stg_tdx_stop_current        <- v2/Bus/Stop/City/{city}
    bus_eta.stg_tdx_route_stop_current  <- v2/Bus/StopOfRoute/City/{city}
    bus_eta.stg_tdx_vehicle_current     <- v2/Bus/Vehicle/City/{city} (optional; skip on failure)

Then run `run_warehouse_sql.py --mode scd2` to hydrate the SCD2 dimensions.
Creds: same file-first loading as the poller (~/.config/bus-eta-logger/tdx.json).
"""
import argparse
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tdx_client import TDXClient  # noqa: E402


def _load_creds():
    """File-first creds (daemon-friendly), keychain fallback — mirrors poller."""
    path = os.environ.get("BUS_ETA_TDX_CREDS_FILE",
                          os.path.expanduser("~/.config/bus-eta-logger/tdx.json"))
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        return d.get("client_id", ""), d.get("client_secret", "")
    import subprocess
    def _kc(acct):
        return subprocess.run(
            ["security", "find-generic-password", "-s", "che-transport-tdx",
             "-a", acct, "-w"],
            capture_output=True, text=True).stdout.strip()
    return _kc("client_id"), _kc("client_secret")


# --- pure mapping functions (unit-tested; no I/O) ---

def map_route(rec, city):
    ops = rec.get("Operators") or []
    return (
        city,
        rec.get("RouteUID"),
        rec.get("RouteID"),
        (rec.get("RouteName") or {}).get("Zh_tw"),
        (rec.get("RouteName") or {}).get("En"),
        rec.get("DepartureStopNameZh"),
        rec.get("DestinationStopNameZh"),
        ops[0].get("OperatorID") if ops else None,
    )


def map_stop(rec, city):
    pos = rec.get("StopPosition") or {}
    return (
        city,
        rec.get("StopUID"),
        rec.get("StopID"),
        (rec.get("StopName") or {}).get("Zh_tw"),
        (rec.get("StopName") or {}).get("En"),
        pos.get("PositionLat"),
        pos.get("PositionLon"),
    )


def map_vehicle(rec):
    vt = rec.get("VehicleType")
    return (
        rec.get("PlateNumb"),
        rec.get("OperatorID"),
        str(vt) if vt is not None else None,
    )


def map_route_stops(rec, city):
    """StopOfRoute entry -> rows (city, route_uid, direction, seq, stop_uid).

    Sub-routes repeat the same (route, dir); caller dedups across entries.
    Entries missing Direction or a Stops list yield nothing.
    """
    route_uid = rec.get("RouteUID")
    direction = rec.get("Direction")
    if route_uid is None or direction is None:
        return []
    rows = []
    for s in rec.get("Stops") or []:
        if s.get("StopUID") is not None and s.get("StopSequence") is not None:
            rows.append((city, route_uid, int(direction),
                         int(s["StopSequence"]), s["StopUID"]))
    return rows


# --- DDL duplicated from 30_scd2_patterns.sql (CREATE IF NOT EXISTS, safe) ---

STAGING_DDL = """
CREATE SCHEMA IF NOT EXISTS bus_eta;
CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_route_current (
    city VARCHAR NOT NULL, route_uid VARCHAR NOT NULL, route_id VARCHAR,
    route_name_zh VARCHAR, route_name_en VARCHAR,
    departure_stop VARCHAR, destination_stop VARCHAR, operator_id VARCHAR);
CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_stop_current (
    city VARCHAR NOT NULL, stop_uid VARCHAR NOT NULL, stop_id VARCHAR,
    stop_name_zh VARCHAR, stop_name_en VARCHAR, stop_lat DOUBLE, stop_lon DOUBLE);
CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_vehicle_current (
    plate VARCHAR NOT NULL, operator_id VARCHAR, vehicle_type VARCHAR);
CREATE TABLE IF NOT EXISTS bus_eta.stg_tdx_route_stop_current (
    city VARCHAR NOT NULL, route_uid VARCHAR NOT NULL, direction INTEGER NOT NULL,
    stop_sequence INTEGER NOT NULL, stop_uid VARCHAR NOT NULL);
"""


def fetch(client, path):
    # explicit $top: guard against any endpoint-side default page size
    return client._get(f"{client.base}{path}?%24format=JSON&%24top=200000")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cities", default="Taipei,NewTaipei")
    args = ap.parse_args(argv)
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]

    cid, csec = _load_creds()
    if not cid or not csec:
        print("ERROR: no TDX credentials", file=sys.stderr)
        return 1
    client = TDXClient(cid, csec)
    client.get_token()

    routes, stops, vehicles, route_stops = [], [], [], set()
    for city in cities:
        data = fetch(client, f"/v2/Bus/Route/City/{city}")
        if data is None:
            print(f"ERROR: Route fetch failed for {city}", file=sys.stderr)
            return 1
        routes += [map_route(r, city) for r in data]

        data = fetch(client, f"/v2/Bus/Stop/City/{city}")
        if data is None:
            print(f"ERROR: Stop fetch failed for {city}", file=sys.stderr)
            return 1
        stops += [map_stop(r, city) for r in data]

        data = fetch(client, f"/v2/Bus/StopOfRoute/City/{city}")
        if data is None:
            print(f"ERROR: StopOfRoute fetch failed for {city}", file=sys.stderr)
            return 1
        for rec in data:
            route_stops.update(map_route_stops(rec, city))

        data = fetch(client, f"/v2/Bus/Vehicle/City/{city}")
        if data is None:
            print(f"WARN: Vehicle fetch failed for {city} (optional; skipping)",
                  file=sys.stderr)
        else:
            vehicles += [map_vehicle(r) for r in data]

    con = duckdb.connect(args.db)
    con.execute(STAGING_DDL)
    con.execute("BEGIN TRANSACTION")
    for table, rows, ph in [
        ("stg_tdx_route_current", routes, 8),
        ("stg_tdx_stop_current", stops, 7),
        ("stg_tdx_vehicle_current",
         [v for v in {v[0]: v for v in vehicles if v[0]}.values()], 3),
        ("stg_tdx_route_stop_current", sorted(route_stops), 5),
    ]:
        con.execute(f"DELETE FROM bus_eta.{table}")
        if rows:
            con.executemany(
                f"INSERT INTO bus_eta.{table} VALUES ({','.join('?' * ph)})", rows)
        print(f"{table}: {len(rows)} rows")
    con.execute("COMMIT")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
