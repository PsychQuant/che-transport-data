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
    notifications are irreversible, so the safe mode is the fallback mode."""

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


def summarize(exc: BaseException, limit: int = 300) -> str:
    """One-line-ish rendering of an exception for the alert body."""
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= limit else text[:limit] + "…"


def report(job: str, ok: bool, detail: str = "", *, sink,
           state_path: str, now: datetime.datetime = None) -> str:
    """Report a job outcome. Returns what actually happened.

    One of: "notified" / "suppressed" / "first-run-ok" / "delivery-failed".

    Never raises. A notifier that can break its subject is worse than no
    notifier at all.
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
        except Exception as exc:
            # Do NOT record the new state — an undelivered alert must be retried
            # next run rather than silently swallowed forever.
            print(f"[notify] delivery failed for {job}: {exc}", file=sys.stderr)
            return "delivery-failed"

        _persist(state_path, state, job, new)
        return "notified"
    except Exception as exc:  # last-resort guard — never propagate to the caller
        print(f"[notify] unexpected failure for {job}: {exc}", file=sys.stderr)
        return "error"


def _persist(state_path: str, state: dict, job: str, new: str) -> None:
    """Best-effort. Losing the write costs at most one redundant alert."""
    try:
        save_state(state_path, {**state, job: new})
    except OSError as exc:
        print(f"[notify] could not persist state to {state_path}: {exc}", file=sys.stderr)
