# Bus ETA Prediction — Methodology

Design notes for the Stage 3+ prediction layer (`bus_eta_predict`) built on the
bus-eta logger's raw thin-facts. Companion to `analysis/spine.sql` (alignment
marts) and `analysis/spacetime.py` (GPS distance-time).

## 1. The metric is TIME (seconds). Not distance, not stops, not speed.

**Principle**: the prediction target, the loss we optimize, and every reported
number are **arrival-time error in seconds** — `predicted_arrival − actual_arrival`.
Position (`s(t)` along the route), stop-sequence, and speed are *internal* modeling
quantities; they are never the metric.

**Why time beats every other unit:**

1. **It is the user's felt experience.** A passenger waits in *time*. "The bus is
   90 s later than predicted" is the thing they experience; "200 m off along the
   route" is not. The metric must match what the prediction is *for*.
2. **It is comparable across the whole system.** A second is a second on every
   route, stop, and segment. Distance error is not interchangeable — 200 m near a
   dense downtown stop ≠ 200 m on a sparse suburban segment (different time, different
   number of stops). Stop-sequence error is ordinal and unevenly spaced. Speed error
   (km/h) is not directly actionable.
3. **It matches the decision to the loss.** People decide "leave now to catch it"
   from time-to-arrival. Optimizing seconds optimizes the actual decision; optimizing
   distance optimizes a proxy.
4. **It aggregates interpretably.** Median |error| = 52 s and p90 = 178 s read
   directly as "half within ~1 min, 90 % within ~3 min". No unit translation.

**Corollary**: we still compute `s(t)` (GPS projection onto the route shape) and
dwell — they capture the *dynamics* that drive arrival time — but we convert to a
time prediction before scoring. Distance is the model's language; time is the score.

## 2. How to report it

Computed by `prediction_error` in `analysis/spine.sql` (`error_sec` is already in
seconds: `predicted_arrival − actual_arrival`, +late / −early).

- **Bias**: mean signed `error_sec` — does the predictor systematically run early or
  late?
- **Dispersion**: median `abs(error_sec)` (MAE-like, robust) and `p90 abs` (tail).
- **Stratify by lead time** (`lead_time_sec`): a prediction made 60 s before arrival
  should be far tighter than one made 600 s before. Always report error *as a function
  of* lead time — a single aggregate number hides this.
- **Stratify by the free covariates** (`arr_hour`, `is_weekend`, `is_holiday`):
  error is not stationary (observed: TDX median |err| ~50 s midday rises to ~110 s at
  15–16 h). Report per stratum, or the headline number is misleading.

## 3. The baseline to beat

TDX's own published ETA, measured by `prediction_error` over captured arrivals:
**median |error| ≈ 52 s, p90 ≈ 178 s** (2026-06-09 sample, route 270 + others). Any
`bus_eta_predict` model is judged by whether it lowers these *seconds*, conditioned on
the same lead-time and covariate strata.

## 4. Related design notes

- Predictability ceiling (why more days ≠ perfect prediction; bias→0 but irreducible
  variance floor remains): measure via held-out `error_sec` vs training-days curve.
- Covariates (no-leakage rule — must be known/forecastable at predict time): calendar
  + dwell are in `spine.sql`; GPS segment-speed / leading-bus (per-route) and a CWA
  weather collector are the next additions.
