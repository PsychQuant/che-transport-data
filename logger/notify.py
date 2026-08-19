"""Failure notification for the bus-eta collection jobs (issue #5).

Two silent failures on 2026-08-12 shared one structure: the signal existed
(exit code + log line) but never reached a person. #3 ran that way for 41 days.

Design constraints, in order of importance:

  1. **Never break the job it watches.** `report()` does not raise — not on a
     dead network, not on an unwritable state file. Notification is an add-on.
  2. **Ring on change, not on state.** A job failing every night must alert
     ONCE. A notifier that cries wolf nightly is one nobody reads, which
     recreates the problem this module exists to solve.
  3. **Decide severity family-wide, here.** All three callers are in the table
     below — including the logger, still blocked on #4 — so the wired ones and
     the pending one cannot drift apart.

Deliberately NOT a launchd shell wrapper: macOS TCC attributes access to the
job's `Program`, so putting `/bin/sh` in front costs the external volume
permission. That is exactly what killed the warehouse job for 41 days (#3).
Each python entry point reports for itself instead.
"""
import datetime
import json
import os
import sys
import tempfile

TPE = datetime.timezone(datetime.timedelta(hours=8))

# job name == launchd label == state key. One table, all three callers.
SEVERITY = {
    "bus-eta-logger": "critical",      # drought loses data permanently, per hour
    "bus-eta-warehouse": "routine",    # backfillable from canonical parquet
    "bus-eta-audit": "routine",        # reports on yesterday; loss already happened
}


def classify_severity(job: str) -> str:
    """Severity for a job name. Unknown jobs are treated as critical.

    Compound keys (`<job>:<sub>`, used for per-(feed,city) `error` conditions)
    resolve to their base job. The unknown-job default exists to catch
    PROGRAMMING ERRORS; relying on it to deliver correct severity for a key
    shape we deliberately introduced would be accidental coverage.

    Never raises: this runs on the caller's ERROR path, where raising would
    swallow the original exception being reported.
    """
    if job in SEVERITY:
        return SEVERITY[job]
    return SEVERITY.get(job.split(":", 1)[0], "critical")


def should_notify(prev, new: str) -> bool:
    """Alert only on a state transition. `prev` is None on the first ever run.

    `fail → fail` returning False is the whole point: without it, #3 would have
    delivered 41 identical alerts and been muted by the second week.
    """
    if prev is None:
        return new == "fail"
    return prev != new


def load_state(path: str) -> dict:
    """{job: "ok"|"fail"}. A missing or corrupted file reads as empty — losing
    state costs one redundant alert, while raising here would break the job."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


SCHEMA_VERSION = 2


def save_state(path: str, state: dict) -> None:
    """Atomic write (tmp + os.replace).

    A plain open("w") truncates first, so a crash mid-write leaves a corrupt
    file that `load_state` reads as empty — and losing state in the "fail"
    direction costs a MISSED recovery, not a redundant alert. Per-run write
    count went from 2 to 8 once `error` conditions became per-(feed,city),
    widening that window fourfold.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".notify-state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def migrate_state(state_path: str, jobs) -> bool:
    """One-time migration to the schema this module now expects.

    Before #7 the audit wrote `state[job] = "fail"` whenever findings existed.
    Now the job level means "did the run complete", so the first run after
    upgrade would read fail→ok and emit the very [RECOVERED] this issue exists
    to eliminate. Drop those legacy job values once.

    Keyed on an explicit `#schema` marker, NOT on "the #seen key is absent" —
    that key is never written under a non-delivering sink (dry-run is the
    current deployment), so such a marker would hold every single night and the
    migration would re-run forever. A schema bump is not a notification
    transition, so it is written unconditionally.

    Returns True if a migration was performed.
    """
    try:
        state = load_state(state_path)
        if state.get("#schema") == SCHEMA_VERSION:
            return False
        cleaned = {k: v for k, v in state.items() if k not in set(jobs)}
        cleaned["#schema"] = SCHEMA_VERSION
        save_state(state_path, cleaned)
        return True
    except BaseException as exc:
        _warn(f"[notify] state migration failed for {state_path}: {exc}")
        return False


def compose_message(job: str, ok: bool, detail: str, now: datetime.datetime) -> str:
    """`now` is injected so the message is testable without touching the clock."""
    status = "RECOVERED" if ok else "FAIL"
    line = f"[{status}] {job} @ {now.isoformat()}"
    return f"{line}\n{detail}" if detail else line


class DryRunSink:
    """Prints instead of delivering. The default when credentials are absent —
    notifications are irreversible, so the safe mode is the fallback mode.

    `delivers = False` is load-bearing: by design (D5) this module ships in
    dry-run until the alerting group exists. If a dry-run "send" counted as
    delivered, every failure in that window would be marked notified while only
    reaching the log nobody reads — and once Telegram is wired, an ongoing
    failure would be suppressed forever as fail→fail. Verify round 1, B2.
    """

    delivers = False

    def send(self, severity: str, message: str) -> None:
        print(f"[notify dry-run/{severity}] {message}", file=sys.stderr)


class DeliveryError(Exception):
    """Delivery failed. Message is guaranteed free of the bot token."""


def redact(text: str, secret) -> str:
    """Strip `secret` out of `text`. No-op when there is no secret."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


class TelegramSink:
    """Telegram delivery.

    ⚠ Telegram puts the bot token in the URL **path**, and httpx embeds the full
    URL in `HTTPStatusError`. Since `report()` prints delivery failures to
    stderr — which lands in bus-eta-*.err.log, a file that lives for months —
    letting that exception through verbatim would turn a single 401 into a
    permanent credential leak. Every error out of this class is redacted first.
    """

    delivers = True

    def __init__(self, token: str, chat_id: str, *, transport=None):
        self.token = token
        self.chat_id = chat_id
        self._transport = transport  # DI seam for offline tests

    def send(self, severity: str, message: str) -> None:
        import httpx  # already a dependency; imported lazily to keep tests offline

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            with httpx.Client(transport=self._transport, timeout=10) as client:
                client.post(
                    url, json={"chat_id": self.chat_id, "text": message}
                ).raise_for_status()
        except Exception as exc:
            # `from None` so no chained traceback can carry the URL either.
            raise DeliveryError(redact(f"{type(exc).__name__}: {exc}", self.token)) from None


def build_sink(token, chat_id):
    """Telegram when fully configured, dry-run otherwise.

    Degrading rather than raising is deliberate: the alerting group does not
    exist yet, and a missing chat_id must not break the jobs being wired.
    """
    if not token or not chat_id:
        print("[notify] no token/chat_id — falling back to dry-run", file=sys.stderr)
        return DryRunSink()
    return TelegramSink(token, chat_id)


def from_env():
    """(sink, state_path) from the environment — shared by every entry point so
    the three callers cannot drift on credential lookup or state location.

    State lives on the SYSTEM disk, not the external NVMe: "the volume is gone"
    is itself a notifiable event, and state stored on a volume that can vanish
    is unreadable exactly when it matters most.
    """
    state_path = os.environ.get(
        "BUS_ETA_NOTIFY_STATE",
        os.path.expanduser("~/.bus-eta-logger/notify-state.json"),
    )
    sink = build_sink(os.environ.get("BUS_ETA_NOTIFY_TOKEN"),
                      os.environ.get("BUS_ETA_NOTIFY_CHAT_ID"))
    return sink, state_path


def is_benign_exit(exc: BaseException) -> bool:
    """True when `exc` ends the process without being a job failure.

    `argparse` signals both `--help` (exit 0) and usage errors via SystemExit,
    and an operator investigating an incident runs `--help` on exactly these
    scripts. Recording that as FAIL poisons the shared state file, so the real
    "the disk is gone" alert that night is suppressed as fail→fail — recreating
    the silence this module exists to remove. Verify round 1, B1.
    """
    if isinstance(exc, KeyboardInterrupt):
        return True
    if isinstance(exc, SystemExit):
        return exc.code in (0, None)
    return False


def summarize(exc: BaseException, limit: int = 300) -> str:
    """One-line-ish rendering of an exception for the alert body."""
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= limit else text[:limit] + "…"


def report(job: str, ok: bool, detail: str = "", *, sink,
           state_path: str, now: datetime.datetime = None) -> str:
    """Report a job outcome. Returns what actually happened.

    One of: "notified" / "suppressed" / "first-run-ok" / "dry-run" /
    "delivery-failed" / "error".

    Never raises — and "never" means `BaseException`, not `Exception`. This
    module's whole argument is that `SystemExit` is not an `Exception`; guarding
    only `Exception` here let a BaseException from the sink escape and replace
    the caller's original SystemExit, destroying the exit code D4 exists to
    preserve. Verify round 1, B3.
    """
    try:
        now = now or datetime.datetime.now(TPE)
        new = "ok" if ok else "fail"
        state = load_state(state_path)
        prev = state.get(job)

        if not should_notify(prev, new):
            _persist(state_path, state, job, new)
            return "first-run-ok" if prev is None else "suppressed"

        try:
            sink.send(classify_severity(job), compose_message(job, ok, detail, now))
        except BaseException as exc:
            # Do NOT record the new state — an undelivered alert must be retried
            # next run rather than silently swallowed forever.
            _warn(f"[notify] delivery failed for {job}: {exc}")
            return "delivery-failed"

        if not getattr(sink, "delivers", True):
            # Printed, not delivered. The transition stays unconsumed so it can
            # still ring once a real sink is configured (B2).
            return "dry-run"

        _persist(state_path, state, job, new)
        return "notified"
    except BaseException as exc:  # last-resort guard — never propagate
        _warn(f"[notify] unexpected failure for {job}: {exc}")
        return "error"


def _warn(message: str) -> None:
    """stderr write that cannot itself raise (closed stream, full disk)."""
    try:
        print(message, file=sys.stderr)
    except BaseException:
        pass


def _persist(state_path: str, state: dict, job: str, new: str) -> None:
    """Best-effort. Losing the write costs at most one redundant alert."""
    try:
        save_state(state_path, {**state, job: new})
    except OSError as exc:
        print(f"[notify] could not persist state to {state_path}: {exc}", file=sys.stderr)


def compose_items_message(job: str, items, now: datetime.datetime) -> str:
    """Message body for newly-seen findings. Lists them — a constant string like
    "partition audit found anomalies" forces the reader back to the log file
    nobody reads, which is the habit this whole module exists to break.
    """
    head = f"[NEW] {job} @ {now.isoformat()} — {len(items)} new finding(s)"
    return head + "\n" + "\n".join(f"  - {i}" for i in items)


def _prune_seen(items, prune_before):
    """Bound the seen-set by the audit window.

    **Precondition (was previously stated as an unconditional truth, wrongly):**
    this is only safe when the window WIDTH is unchanged between runs. At a
    fixed width the window only moves forward, so a pruned item cannot re-enter
    findings. At a different width a narrower run would prune items a wider run
    still finds — and CLAUDE.md documents a manual `--window-days 0` check
    against the same state file. The caller (`report_new`) enforces the width
    check; this function assumes it was done.

    Items whose last path segment is not an ISO date are kept.
    """
    if not prune_before:
        return list(items)
    kept = []
    for item in items:
        tail = item.rsplit("/", 1)[-1]
        try:
            datetime.date.fromisoformat(tail)
        except ValueError:
            kept.append(item)          # no date component — never prune
            continue
        if tail >= prune_before:
            kept.append(item)
    return kept


def _persist_keys(state_path: str, state: dict, updates: dict) -> None:
    """Best-effort write of several keys in ONE atomic replace."""
    try:
        save_state(state_path, {**state, **updates})
    except OSError as exc:
        _warn(f"[notify] could not persist state to {state_path}: {exc}")


def _chunk_items(job: str, items, now, budget: int):
    """Split items so each composed message stays under `budget` characters.

    Telegram rejects >4096 with HTTP 400. Combined with the (correct) rule that
    an undelivered alert must not consume the seen-set, an oversized message
    retries identically forever — so the bigger the outage, the more certain
    the alert never arrives.
    """
    head = len(compose_items_message(job, [], now)) + 8   # margin for count digits
    chunks, cur, size = [], [], head
    for item in items:
        line = len(f"\n  - {item}")
        if cur and size + line > budget:
            chunks.append(cur)
            cur, size = [], head
        cur.append(item)
        size += line
    if cur:
        chunks.append(cur)
    return chunks


def _persist_key(state_path: str, state: dict, key: str, value) -> None:
    """Best-effort write of ONE key. `{**state, key: value}` preserves every
    other key, so `report()` and `report_new()` can share a file safely."""
    try:
        save_state(state_path, {**state, key: value})
    except OSError as exc:
        _warn(f"[notify] could not persist state to {state_path}: {exc}")


def report_new(job: str, items, *, sink, state_path: str,
               now: datetime.datetime = None, prune_before=None,
               window_days=None, char_budget: int = 3500) -> dict:
    """Alert once per previously-unseen item, then stay quiet (issue #7).

    Correct for findings that are permanent facts about a past date
    (`missing`, `low_volume`). **Not** correct for a condition describing the
    CURRENT state — `error` ("this feed has no partitions at all") genuinely
    recovers and genuinely recurs, so it goes through `report()` instead. The
    three kinds are enumerated in `audit_partitions.KIND_SEMANTICS`; adding a
    fourth means adding a row, not generalising the rule. Round 1 of this issue
    failed verify precisely because one row was generalised into a law.

    Returns {"status", "alerted", "already_seen"}; status is one of
    "alerted" / "partial" / "silent" / "dry-run" / "delivery-failed" / "error".
    Never raises.
    """
    try:
        now = now or datetime.datetime.now(TPE)
        key, wkey = f"{job}#seen", f"{job}#window"
        state = load_state(state_path)
        raw = state.get(key)
        seen = [str(x) for x in raw] if isinstance(raw, list) else []

        items = list(dict.fromkeys(items))
        already = set(seen)
        new_items = [i for i in items if i not in already]

        # DP5 — prune only at an unchanged window width (see _prune_seen).
        may_prune = bool(prune_before) and window_days is not None \
            and state.get(wkey) in (None, window_days)
        if prune_before and not may_prune:
            _warn(f"[notify] {job}: window width {state.get(wkey)} -> {window_days}"
                  " — skipping prune (a narrower run must not drop what a wider one finds)")

        def _write(all_items):
            updates = {key: _prune_seen(all_items, prune_before) if may_prune
                       else list(all_items)}
            if may_prune:
                updates[wkey] = window_days
            _persist_keys(state_path, state, updates)

        if not new_items:
            if may_prune and (_prune_seen(seen, prune_before) != seen
                              or state.get(wkey) != window_days):
                _write(seen)
            return {"status": "silent", "alerted": [], "already_seen": items}

        delivered, failed = [], False
        for chunk in _chunk_items(job, new_items, now, char_budget):
            try:
                sink.send(classify_severity(job), compose_items_message(job, chunk, now))
            except BaseException as exc:
                _warn(f"[notify] delivery failed for {job}: {exc}")
                failed = True
                break
            delivered.extend(chunk)

        if not getattr(sink, "delivers", True):
            return {"status": "dry-run", "alerted": [], "already_seen": items}
        if not delivered:
            return {"status": "delivery-failed", "alerted": [], "already_seen": items}

        # Consume ONLY what actually went out; the remainder retries next run.
        _write(seen + delivered)
        return {"status": "partial" if failed else "alerted",
                "alerted": delivered,
                "already_seen": [i for i in items if i in already]}
    except BaseException as exc:                    # last-resort — never propagate
        _warn(f"[notify] unexpected failure for {job}: {exc}")
        return {"status": "error", "alerted": [], "already_seen": []}
