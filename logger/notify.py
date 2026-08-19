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

TPE = datetime.timezone(datetime.timedelta(hours=8))

# job name == launchd label == state key. One table, all three callers.
SEVERITY = {
    "bus-eta-logger": "critical",      # drought loses data permanently, per hour
    "bus-eta-warehouse": "routine",    # backfillable from canonical parquet
    "bus-eta-audit": "routine",        # reports on yesterday; loss already happened
}


def classify_severity(job: str) -> str:
    """Severity for a job name. Unknown jobs are treated as critical.

    Never raises: this runs on the caller's ERROR path, where raising would
    swallow the original exception being reported.
    """
    return SEVERITY.get(job, "critical")


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


def save_state(path: str, state: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f)


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

    Safe because the window only moves forward: a pruned item cannot re-enter
    findings, so pruning can never cause a duplicate alert. Items whose last
    path segment is not an ISO date (e.g. `<feed>/<city>/error`) are kept.
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


def _persist_key(state_path: str, state: dict, key: str, value) -> None:
    """Best-effort write of ONE key. `{**state, key: value}` preserves every
    other key, so `report()` and `report_new()` can share a file safely."""
    try:
        save_state(state_path, {**state, key: value})
    except OSError as exc:
        _warn(f"[notify] could not persist state to {state_path}: {exc}")


def report_new(job: str, items, *, sink, state_path: str,
               now: datetime.datetime = None, prune_before=None) -> dict:
    """Alert once per previously-unseen item, then stay quiet (issue #7).

    For a job whose outcome is a SET of findings rather than a boolean, the
    ok/fail state machine is the wrong tool — not broken, wrong-shaped. It lost
    information in both directions:

      - a set that emptied because the audit window slid read as `fail → ok`,
        producing a `[RECOVERED]` that is semantically always false (TDX retains
        ~2h; a hole is permanent, nothing recovers)
      - a set that grew while staying non-empty read as `fail → fail`, so a
        disaster tripling in size stayed completely silent

    Alerting per distinct item removes both at once: there is no recovery to
    announce, and a bigger disaster contains new items.

    `report()` is untouched and remains correct for the genuinely-binary callers
    (warehouse: the run completed or raised; logger: data is flowing or not).
    Both keep working, and both DO have real recoveries worth announcing.

    Returns {"status": ..., "alerted": [...], "already_seen": [...]} where status
    is one of "alerted" / "silent" / "dry-run" / "delivery-failed" / "error".
    Never raises.
    """
    try:
        now = now or datetime.datetime.now(TPE)
        key = f"{job}#seen"
        state = load_state(state_path)
        raw = state.get(key)
        seen = [str(x) for x in raw] if isinstance(raw, list) else []

        items = list(dict.fromkeys(items))          # dedupe, keep caller order
        already = set(seen)
        new = [i for i in items if i not in already]
        kept = _prune_seen(seen + new, prune_before)

        if not new:
            if kept != seen:                        # pruning still worth writing
                _persist_key(state_path, state, key, kept)
            return {"status": "silent", "alerted": [], "already_seen": items}

        try:
            sink.send(classify_severity(job), compose_items_message(job, new, now))
        except BaseException as exc:
            # Undelivered → do NOT consume the seen-set; retry next run.
            _warn(f"[notify] delivery failed for {job}: {exc}")
            return {"status": "delivery-failed", "alerted": [], "already_seen": items}

        if not getattr(sink, "delivers", True):
            return {"status": "dry-run", "alerted": [], "already_seen": items}

        _persist_key(state_path, state, key, kept)
        return {"status": "alerted", "alerted": new,
                "already_seen": [i for i in items if i in already]}
    except BaseException as exc:                    # last-resort — never propagate
        _warn(f"[notify] unexpected failure for {job}: {exc}")
        return {"status": "error", "alerted": [], "already_seen": []}
