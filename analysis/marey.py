#!/usr/bin/env python3
"""Marey (time-space) diagram for a Taiwan bus route, from captured arrival_event.

Each line = one trip (origin stop 1 -> terminal); slope = speed; vertical gap
between lines = headway. Built from the bus-eta logger's A2 arrival_event store.

Usage (run on the laptop; needs matplotlib + httpx + the TDX keychain creds):
    python analysis/marey.py 270
    python analysis/marey.py 299 --city NewTaipei --out /tmp/m299.png
    python analysis/marey.py 270 --local /path/to/parquet        # query local Parquet
By default it queries the mini over ssh (data lives on the NVMe there).
"""
import argparse, csv, collections, datetime, io, shlex, subprocess, sys, time

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
BASE = "https://tdx.transportdata.tw/api/basic"
KC_SERVICE = "che-transport-tdx"
SSH_HOST = "mini-che"
DATA_ROOT = "/Volumes/mini-2TB-SSD/che-transport/bus-eta/parquet"
REMOTE_PY = "~/bus-eta-logger/.venv/bin/python"

# remote/local query: emit CSV of (plate, stop_sequence, direction, epoch) for a route's arrivals
_QUERY = """
import duckdb, os, sys, csv
root=os.environ["ROOT"]; uids=os.environ["UIDS"].split(",")
rel=f"read_parquet('{root}/arrival_event/**/*.parquet',hive_partitioning=true)"
inlist=",".join("'"+u+"'" for u in uids)
rows=duckdb.sql(f"select plate,stop_sequence,direction,epoch(gps_time) t from {rel} "
                f"where route_uid in ({inlist}) and event_type=1 and plate is not null "
                f"and stop_sequence is not null order by plate,t").fetchall()
w=csv.writer(sys.stdout); w.writerow(["plate","stop_sequence","direction","t"])
for r in rows: w.writerow(r)
"""


def _kc(acct):
    return subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", acct, "-w"],
                          capture_output=True, text=True).stdout.strip()


def resolve_route_uids(city, name):
    import httpx
    tok = httpx.post(TOKEN_URL, data={"grant_type": "client_credentials",
                     "client_id": _kc("client_id"), "client_secret": _kc("client_secret")},
                     timeout=20).json()["access_token"]
    h = {"authorization": f"Bearer {tok}"}
    for attempt in range(6):
        r = httpx.get(f"{BASE}/v2/Bus/Route/City/{city}",
                      params={"$filter": f"RouteName/Zh_tw eq '{name}'", "$format": "JSON"},
                      headers=h, timeout=30)
        if r.status_code == 200:
            return sorted({d["RouteUID"] for d in r.json()})
        print(f"  TDX {r.status_code} resolving '{name}', retry in 8s", file=sys.stderr); time.sleep(8)
    raise SystemExit("TDX rate-limited; retry later (logger shares the key)")


def fetch_rows(uids, local_root):
    env = {"ROOT": local_root or DATA_ROOT, "UIDS": ",".join(uids)}
    if local_root:
        import os
        os.environ.update(env)
        out = subprocess.run([sys.executable, "-c", _QUERY], capture_output=True, text=True)
    else:
        envset = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        out = subprocess.run(["ssh", SSH_HOST, f"{envset} {REMOTE_PY} -"],
                             input=_QUERY, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"query failed:\n{out.stderr}")
    return list(csv.DictReader(io.StringIO(out.stdout)))


def split_trips(rows):
    bykey = collections.defaultdict(list)
    for r in rows:
        bykey[(r["plate"], int(float(r["direction"])))].append((float(r["t"]), int(float(r["stop_sequence"]))))
    trips = collections.defaultdict(list)
    for (plate, d), pts in bykey.items():
        pts.sort(); trip = 0; ps = pt = None
        for t, s in pts:
            if ps is not None and (s < ps or t - pt > 1800):  # seq reset or >30min gap = new trip
                trip += 1
            trips[(plate, d, trip)].append((t, s)); ps, pt = s, t
    return trips, sorted({k[0] for k in bykey})


def plot(trips, plates, name, out):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt, matplotlib.dates as mdates
    pidx = {p: i for i, p in enumerate(plates)}; cmap = plt.cm.tab20
    dirs = sorted({k[1] for k in trips})
    fig, axes = plt.subplots(1, len(dirs), figsize=(9 * len(dirs), 8), sharey=True, squeeze=False)
    for ax, d in zip(axes[0], dirs):
        nt = 0
        for (plate, dd, trip), pts in trips.items():
            if dd != d:
                continue
            pts.sort(); xs = [datetime.datetime.fromtimestamp(t) for t, _ in pts]; ys = [s for _, s in pts]
            ax.plot(xs, ys, "-", lw=1.1, alpha=0.85, color=cmap(pidx[plate] % 20)); nt += 1
        nb = len({k[0] for k in trips if k[1] == d})
        ax.set_title(f"Direction {d}  ({nt} trips, {nb} buses)")
        ax.set_xlabel("Time"); ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[0][0].set_ylabel("Stop sequence (origin 1 -> terminal)")
    fig.suptitle(f"Bus {name} - Marey diagram  |  line=one trip, slope=speed, vertical gap=headway", fontsize=13)
    plt.tight_layout(); plt.savefig(out, dpi=110, bbox_inches="tight")
    print(f"saved {out}  ({len(trips)} trips, {len(plates)} buses)")


def main():
    ap = argparse.ArgumentParser(description="Marey time-space diagram for a bus route")
    ap.add_argument("route", help="route name, e.g. 270")
    ap.add_argument("--city", default="Taipei")
    ap.add_argument("--out", default=None)
    ap.add_argument("--local", default=None, help="local Parquet root (else query mini over ssh)")
    a = ap.parse_args()
    uids = resolve_route_uids(a.city, a.route)
    if not uids:
        raise SystemExit(f"no route named '{a.route}' in {a.city}")
    print(f"route '{a.route}' -> {uids}", file=sys.stderr)
    rows = fetch_rows(uids, a.local)
    if not rows:
        raise SystemExit("no arrival_event rows captured for this route yet")
    trips, plates = split_trips(rows)
    plot(trips, plates, a.route, a.out or f"marey_{a.route}.png")


if __name__ == "__main__":
    main()
