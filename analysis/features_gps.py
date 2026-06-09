#!/usr/bin/env python3
"""GPS-derived covariates for a bus route: segment speed (endogenous traffic
proxy) + leading-bus headway. Built on spacetime.py's route-shape projection.

Usage (laptop): python analysis/features_gps.py 270 [--out speed_270.png]
- segment speed: along-route Δs/Δt of A1 GPS, binned (segment x time) -> the
  "endogenous congestion map" (the route tells you its own traffic). Rendered
  as a heatmap; also the recent-segment-speed covariate for bus_eta_predict.
- leading-bus headway: at time snapshots, per direction, the along-route gap to
  the bus ahead (interpolated s) -> headway covariate. Summary printed.
These are causal (past-only) features -> no leakage when used at predict time.
"""
import argparse, collections, sys
from marey import resolve_route_uids
from spacetime import fetch_shape, fetch_a1, build_polylines, project_points

SEG_M = 500     # route segment length for speed bins (m)
TWIN_S = 300    # time window for speed bins (s)
MAX_KMH = 90    # drop faster samples (GPS glitch)
GAP_S = 120     # no speed/interp across gaps longer than this (s)
SNAP_S = 60     # headway snapshot interval (s)


def speeds_from_traj(bv):
    out = collections.defaultdict(list)
    for (pl, d), pts in bv.items():
        pts = sorted(pts)
        for (t0, s0), (t1, s1) in zip(pts, pts[1:]):
            dt, ds = t1 - t0, s1 - s0
            if dt <= 0 or dt > GAP_S or ds < 0:
                continue
            kmh = ds / dt * 3.6
            if kmh > MAX_KMH:
                continue
            out[(pl, d)].append(((t0 + t1) / 2, (s0 + s1) / 2, kmh))
    return out


def segment_grid(speeds, t0):
    grid = collections.defaultdict(lambda: collections.defaultdict(list))
    for (pl, d), samp in speeds.items():
        for tm, sm, kmh in samp:
            grid[d][(int(sm // SEG_M), int((tm - t0) // TWIN_S))].append(kmh)
    return grid


def headways(bv):
    bydir = collections.defaultdict(dict); tmin = tmax = None
    for (pl, d), pts in bv.items():
        pts = sorted(pts); bydir[d][pl] = pts
        tmin = pts[0][0] if tmin is None else min(tmin, pts[0][0])
        tmax = pts[-1][0] if tmax is None else max(tmax, pts[-1][0])

    def interp(pts, t):
        for (t0, s0), (t1, s1) in zip(pts, pts[1:]):
            if t0 <= t <= t1 and t1 - t0 <= GAP_S:
                return s0 + (s1 - s0) * (t - t0) / (t1 - t0) if t1 > t0 else s0
        return None

    res = collections.defaultdict(list); t = tmin
    while tmin is not None and t <= tmax:
        for d, buses in bydir.items():
            cur = sorted(s for s in (interp(p, t) for p in buses.values()) if s is not None)
            res[d] += [b - a for a, b in zip(cur, cur[1:])]   # along-route gap to leader (m)
        t += SNAP_S
    return res


def plot_heatmap(grid, cum, name, out, t0):
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt, numpy as np, datetime
    dirs = sorted(grid)
    fig, axes = plt.subplots(1, len(dirs), figsize=(9 * len(dirs), 8), squeeze=False)
    for ax, d in zip(axes[0], dirs):
        cells = grid[d]
        if not cells:
            continue
        nseg = max(k[0] for k in cells) + 1; ntb = max(k[1] for k in cells) + 1
        M = np.full((nseg, ntb), np.nan)
        for (sg, tb), v in cells.items():
            M[sg, tb] = sum(v) / len(v)
        im = ax.imshow(M, origin="lower", aspect="auto", cmap="RdYlGn", vmin=0, vmax=40,
                       extent=[0, ntb * TWIN_S / 3600, 0, nseg * SEG_M / 1000])
        start = datetime.datetime.fromtimestamp(t0).strftime("%H:%M")
        ax.set_title(f"Direction {d}  ({cum[d][-1]/1000:.1f} km)")
        ax.set_xlabel(f"Hours since {start}"); ax.set_ylabel("Route distance (km)")
        fig.colorbar(im, ax=ax, label="mean bus speed (km/h)")
    fig.suptitle(f"Bus {name} - segment speed map (endogenous traffic; red=slow, green=fast)", fontsize=13)
    plt.tight_layout(); plt.savefig(out, dpi=110, bbox_inches="tight"); print(f"saved {out}")


def main():
    ap = argparse.ArgumentParser(description="GPS segment-speed + leading-bus headway covariates")
    ap.add_argument("route"); ap.add_argument("--city", default="Taipei")
    ap.add_argument("--out", default=None); ap.add_argument("--local", default=None)
    a = ap.parse_args()
    uids = resolve_route_uids(a.city, a.route)
    if not uids:
        raise SystemExit(f"no route '{a.route}' in {a.city}")
    print(f"route '{a.route}' -> {uids}", file=sys.stderr)
    shape = fetch_shape(a.city, a.route)
    to_m, polym, cum = build_polylines(shape)
    rows = fetch_a1(uids, a.local)
    bv, n_off_service, n_off_route = project_points(rows, to_m, polym, cum, monotonic=True)
    speeds = speeds_from_traj(bv)
    all_t = [tm for samp in speeds.values() for tm, _, _ in samp]
    if not all_t:
        raise SystemExit("no speed samples")
    t0 = min(all_t)
    grid = segment_grid(speeds, t0)
    hw = headways(bv)
    # summaries (the covariates)
    import statistics
    flat = [(d, sg, tb, sum(v) / len(v)) for d in grid for (sg, tb), v in grid[d].items() if len(v) >= 2]
    slow = sorted(flat, key=lambda x: x[3])[:5]
    print("最慢的 段×時 cell (dir, km, hr-from-start, km/h):",
          [(d, sg * SEG_M / 1000, round(tb * TWIN_S / 3600, 1), round(v, 1)) for d, sg, tb, v in slow], file=sys.stderr)
    for d in sorted(hw):
        h = [x for x in hw[d] if x > 0]
        if h:
            print(f"dir{d} headway(沿線車距 m): 中位 {statistics.median(h):.0f}, p10 {sorted(h)[len(h)//10]:.0f}", file=sys.stderr)
    plot_heatmap(grid, cum, a.route, a.out or f"speed_{a.route}.png", t0)


if __name__ == "__main__":
    main()
