#!/usr/bin/env python3
"""GPS distance-time (time-space) diagram for a bus route, with cleaning.

Pipeline (vs marey.py which uses ordinal stop_sequence, this uses real distance):
  A1 vehicle_position (route_uid)
   -> filter in-service (duty_status=1 & bus_status=0)        # drop depot/off-service
   -> project each GPS point onto the route Shape polyline    # s(t) = arc-length (m)
   -> split into trips (s reset or time gap)
   -> keep clean trips (coverage >= --min-coverage, forward-rate >= --min-forward)
   -> plot distance-time (slope = REAL km/h); report dropped/anomalous trips

Usage (run on laptop; needs matplotlib + httpx + TDX keychain creds):
  python analysis/spacetime.py 270
  python analysis/spacetime.py 270 --keep-anomalies --out /tmp/s270.png
  python analysis/spacetime.py 299 --city NewTaipei --local /path/to/parquet
"""
import argparse, collections, csv, datetime, io, math, shlex, subprocess, sys, time
from marey import _kc, resolve_route_uids, TOKEN_URL, BASE, SSH_HOST, DATA_ROOT, REMOTE_PY

OFF_ROUTE_M = 200      # drop GPS points farther than this from the route line (noise / off-route)
SPLIT_BACK_M = 1500    # s going backward more than this -> new trip
SPLIT_GAP_S = 1800     # time gap > this -> new trip

_A1_QUERY = """
import duckdb, os, sys, csv
root=os.environ["ROOT"]; uids=os.environ["UIDS"].split(",")
rel=f"read_parquet('{root}/vehicle_position/**/*.parquet',hive_partitioning=true)"
inlist=",".join("'"+u+"'" for u in uids)
rows=duckdb.sql(f"select plate,direction,gps_lat,gps_lon,duty_status,bus_status,epoch(gps_time) t "
                f"from {rel} where route_uid in ({inlist}) and gps_lat is not null order by plate,t").fetchall()
w=csv.writer(sys.stdout); w.writerow(["plate","direction","gps_lat","gps_lon","duty_status","bus_status","t"])
for r in rows: w.writerow(r)
"""


def _parse_ls(wkt):
    inner = wkt[wkt.index("(") + 1:wkt.rindex(")")]
    return [tuple(map(float, pr.strip().split())) for pr in inner.split(",")]


def fetch_shape(city, name):
    import httpx
    tok = httpx.post(TOKEN_URL, data={"grant_type": "client_credentials",
          "client_id": _kc("client_id"), "client_secret": _kc("client_secret")}, timeout=20).json()["access_token"]
    h = {"authorization": f"Bearer {tok}"}
    for _ in range(6):
        r = httpx.get(f"{BASE}/v2/Bus/Shape/City/{city}",
                      params={"$filter": f"RouteName/Zh_tw eq '{name}'", "$format": "JSON"}, headers=h, timeout=30)
        if r.status_code == 200:
            return {int(s["Direction"]): _parse_ls(s["Geometry"]) for s in r.json()}
        print(f"  TDX {r.status_code} fetching shape, retry 8s", file=sys.stderr); time.sleep(8)
    raise SystemExit("TDX rate-limited fetching shape; retry later")


def fetch_a1(uids, local_root):
    env = {"ROOT": local_root or DATA_ROOT, "UIDS": ",".join(uids)}
    if local_root:
        import os; os.environ.update(env)
        out = subprocess.run([sys.executable, "-c", _A1_QUERY], capture_output=True, text=True)
    else:
        envset = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        out = subprocess.run(["ssh", SSH_HOST, f"{envset} {REMOTE_PY} -"], input=_A1_QUERY, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"A1 query failed:\n{out.stderr}")
    return list(csv.DictReader(io.StringIO(out.stdout)))


def build_polylines(shape):
    allp = [p for v in shape.values() for p in v]
    lon0 = sum(p[0] for p in allp) / len(allp); lat0 = sum(p[1] for p in allp) / len(allp)
    kx = math.cos(math.radians(lat0)) * 111320.0; ky = 110540.0
    to_m = lambda lo, la: ((lo - lon0) * kx, (la - lat0) * ky)
    polym, cum = {}, {}
    for d, v in shape.items():
        m = [to_m(*p) for p in v]; polym[d] = m; c = [0.0]
        for i in range(len(m) - 1):
            c.append(c[-1] + math.dist(m[i], m[i + 1]))
        cum[d] = c
    return to_m, polym, cum


def project(px, py, m, c):
    """Return (arc-length s along the polyline, perpendicular distance) for the nearest point."""
    bs, bd = None, 1e18
    for i in range(len(m) - 1):
        ax, ay = m[i]; bx, by = m[i + 1]; dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        cx, cy = ax + t * dx, ay + t * dy; dd = (px - cx) ** 2 + (py - cy) ** 2
        if dd < bd:
            bd = dd; bs = c[i] + t * math.sqrt(L2)
    return bs, math.sqrt(bd)


def split_and_score(bv, cum, min_cov, min_fwd):
    trips = collections.defaultdict(list)
    for (pl, d), pts in bv.items():
        pts.sort(); k = 0; ps = pt = None
        for t, s in pts:
            if ps is not None and (s < ps - SPLIT_BACK_M or t - pt > SPLIT_GAP_S):
                k += 1
            trips[(pl, d, k)].append((t, s)); ps, pt = s, t
    clean, dropped = {}, {}
    for key, pts in trips.items():
        d = key[1]; ss = [s for _, s in pts]
        cov = (max(ss) - min(ss)) / cum[d][-1] if len(ss) > 1 else 0.0
        fwd = sum(1 for x, y in zip(ss, ss[1:]) if y >= x) / max(len(ss) - 1, 1)
        (clean if (cov >= min_cov and fwd >= min_fwd) else dropped)[key] = pts
    return clean, dropped


def plot(clean, dropped, cum, name, out, keep):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt, matplotlib.dates as mdates
    plates = sorted({k[0] for k in clean}); pidx = {p: i for i, p in enumerate(plates)}; cmap = plt.cm.tab20
    dirs = sorted(cum)
    fig, axes = plt.subplots(1, len(dirs), figsize=(9 * len(dirs), 8), sharey=True, squeeze=False)
    for ax, d in zip(axes[0], dirs):
        if keep:
            for (pl, dd, k), pts in dropped.items():
                if dd != d:
                    continue
                pts = sorted(pts)
                ax.plot([datetime.datetime.fromtimestamp(t) for t, _ in pts], [s / 1000 for _, s in pts],
                        "-", lw=0.8, alpha=0.25, color="grey")
        nt = 0
        for (pl, dd, k), pts in clean.items():
            if dd != d:
                continue
            pts = sorted(pts)
            ax.plot([datetime.datetime.fromtimestamp(t) for t, _ in pts], [s / 1000 for _, s in pts],
                    "-", lw=1.4, alpha=0.9, color=cmap(pidx[pl] % 20)); nt += 1
        ax.set_title(f"Direction {d}  ({nt} clean trips)  route {cum[d][-1]/1000:.1f} km")
        ax.set_xlabel("Time"); ax.grid(True, alpha=0.3); ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axes[0][0].set_ylabel("s(t) = distance along route (km, from GPS)")
    extra = "  (grey = dropped/anomalous)" if keep else ""
    fig.suptitle(f"Bus {name} - GPS distance-time, clean trips{extra}  |  slope = real speed (km/h)", fontsize=13)
    plt.tight_layout(); plt.savefig(out, dpi=110, bbox_inches="tight")
    print(f"saved {out}  ({len(clean)} clean, {len(dropped)} dropped)")


def main():
    ap = argparse.ArgumentParser(description="GPS distance-time diagram for a bus route, cleaned")
    ap.add_argument("route"); ap.add_argument("--city", default="Taipei")
    ap.add_argument("--out", default=None); ap.add_argument("--local", default=None, help="local Parquet root (else ssh mini)")
    ap.add_argument("--min-coverage", type=float, default=0.8, help="keep trips covering >= this fraction of route")
    ap.add_argument("--min-forward", type=float, default=0.8, help="keep trips with >= this fraction forward movement")
    ap.add_argument("--keep-anomalies", action="store_true", help="also draw dropped trips in grey")
    a = ap.parse_args()
    uids = resolve_route_uids(a.city, a.route)
    if not uids:
        raise SystemExit(f"no route '{a.route}' in {a.city}")
    print(f"route '{a.route}' -> {uids}", file=sys.stderr)
    shape = fetch_shape(a.city, a.route)
    to_m, polym, cum = build_polylines(shape)
    rows = fetch_a1(uids, a.local)
    bv = collections.defaultdict(list); n_off_service = n_off_route = 0
    for r in rows:
        if not (r["duty_status"] == "1" and r["bus_status"] == "0"):
            n_off_service += 1; continue
        d = int(float(r["direction"]))
        if d not in polym:
            continue
        s, perp = project(*to_m(float(r["gps_lon"]), float(r["gps_lat"])), polym[d], cum[d])
        if perp > OFF_ROUTE_M:
            n_off_route += 1; continue
        bv[(r["plate"], d)].append((float(r["t"]), s))
    clean, dropped = split_and_score(bv, cum, a.min_coverage, a.min_forward)
    print(f"points dropped: off-service {n_off_service}, off-route(>{OFF_ROUTE_M}m) {n_off_route}", file=sys.stderr)
    print(f"trips: {len(clean)} clean, {len(dropped)} dropped (cov<{a.min_coverage:.0%} or fwd<{a.min_forward:.0%})", file=sys.stderr)
    plot(clean, dropped, cum, a.route, a.out or f"spacetime_{a.route}.png", a.keep_anomalies)


if __name__ == "__main__":
    main()
