"""Contract for failure notification (issue #5).

Two silent failures on 2026-08-12 shared one structure: the signal existed
(exit code + log line) but never reached a person. #3 ran for 41 days that way.

The design constraints these tests pin down:

  - State machine, not repeat-alerting. A job that fails every night must ring
    ONCE. A notifier that cries wolf nightly is a notifier nobody reads, which
    recreates the very problem this module exists to solve.
  - `report()` must never raise. Notification is an add-on; it must not be able
    to break the job it watches.
  - Severity is decided family-wide here, for all three future callers, so the
    two wired today and the one blocked on #4 cannot drift apart.
"""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import notify  # noqa: E402


NOW = datetime.datetime(2026, 8, 13, 3, 30, tzinfo=notify.TPE)


class RecordingSink:
    """Real object, not a mock — records what it was asked to send."""

    def __init__(self, explode=False):
        self.sent = []
        self.explode = explode

    def send(self, severity, message):
        if self.explode:
            raise RuntimeError("delivery exploded")
        self.sent.append((severity, message))


# ── severity: decided family-wide, for all three callers ───────────────────
def test_logger_is_critical():
    # A logger drought loses data permanently for every hour it goes unnoticed.
    assert notify.classify_severity("bus-eta-logger") == "critical"


def test_warehouse_is_routine():
    # Backfillable from the canonical parquet — no need to wake anyone at 03:30.
    assert notify.classify_severity("bus-eta-warehouse") == "routine"


def test_audit_is_routine():
    # The audit reports on yesterday; the loss already happened.
    assert notify.classify_severity("bus-eta-audit") == "routine"


def test_unknown_job_is_critical_and_does_not_raise():
    # classify_severity runs ON THE ERROR PATH. Raising here would swallow the
    # original exception the caller was trying to report.
    assert notify.classify_severity("something-new") == "critical"


# ── state machine: the anti-noise core ─────────────────────────────────────
def test_first_run_healthy_is_silent():
    assert notify.should_notify(None, "ok") is False


def test_first_observation_being_a_failure_notifies():
    assert notify.should_notify(None, "fail") is True


def test_transition_into_failure_notifies():
    assert notify.should_notify("ok", "fail") is True


def test_repeat_failure_is_suppressed():
    # THE requirement. Without this, #3 would have rung 41 times.
    assert notify.should_notify("fail", "fail") is False


def test_recovery_notifies():
    assert notify.should_notify("fail", "ok") is True


def test_steady_healthy_is_silent():
    assert notify.should_notify("ok", "ok") is False


# ── state persistence ──────────────────────────────────────────────────────
def test_state_round_trips(tmp_path):
    p = tmp_path / "notify-state.json"
    notify.save_state(str(p), {"bus-eta-audit": "fail"})
    assert notify.load_state(str(p)) == {"bus-eta-audit": "fail"}


def test_missing_state_file_reads_as_empty(tmp_path):
    assert notify.load_state(str(tmp_path / "nope.json")) == {}


def test_corrupted_state_file_reads_as_empty_rather_than_crashing(tmp_path):
    p = tmp_path / "notify-state.json"
    p.write_text("{ this is not json")
    assert notify.load_state(str(p)) == {}


def test_save_creates_missing_parent_directory(tmp_path):
    p = tmp_path / "deep" / "nested" / "notify-state.json"
    notify.save_state(str(p), {"j": "ok"})
    assert json.loads(p.read_text()) == {"j": "ok"}


# ── message composition (clock injected, never read) ───────────────────────
def test_message_carries_job_status_detail_and_timestamp():
    msg = notify.compose_message("bus-eta-audit", ok=False, detail="3 days missing", now=NOW)
    assert "bus-eta-audit" in msg
    assert "3 days missing" in msg
    assert "2026-08-13" in msg


def test_recovery_message_reads_as_recovery():
    msg = notify.compose_message("bus-eta-warehouse", ok=True, detail="", now=NOW)
    assert "bus-eta-warehouse" in msg
    assert "FAIL" not in msg


# ── report(): the only side-effecting entry point ──────────────────────────
def test_report_notifies_on_first_failure(tmp_path):
    sink = RecordingSink()
    action = notify.report("bus-eta-audit", ok=False, detail="d",
                           sink=sink, state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "notified"
    assert len(sink.sent) == 1
    assert sink.sent[0][0] == "routine"


def test_report_is_silent_on_first_healthy_run(tmp_path):
    sink = RecordingSink()
    action = notify.report("bus-eta-audit", ok=True, detail="",
                           sink=sink, state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "first-run-ok"
    assert sink.sent == []


def test_report_persists_state_across_calls(tmp_path):
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report("bus-eta-audit", ok=False, detail="d", sink=sink, state_path=sp, now=NOW)
    action = notify.report("bus-eta-audit", ok=False, detail="d", sink=sink, state_path=sp, now=NOW)
    assert action == "suppressed"
    assert len(sink.sent) == 1  # still one — the second call sent nothing


def test_report_notifies_on_recovery(tmp_path):
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report("bus-eta-audit", ok=False, detail="d", sink=sink, state_path=sp, now=NOW)
    action = notify.report("bus-eta-audit", ok=True, detail="", sink=sink, state_path=sp, now=NOW)
    assert action == "notified"
    assert len(sink.sent) == 2


def test_jobs_have_independent_state(tmp_path):
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report("bus-eta-audit", ok=False, detail="d", sink=sink, state_path=sp, now=NOW)
    # A different job failing must still notify — state is keyed per job.
    action = notify.report("bus-eta-warehouse", ok=False, detail="d",
                           sink=sink, state_path=sp, now=NOW)
    assert action == "notified"


def test_report_never_raises_when_delivery_fails(tmp_path):
    # A broken notifier must not break the job it watches.
    sink = RecordingSink(explode=True)
    action = notify.report("bus-eta-audit", ok=False, detail="d",
                           sink=sink, state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "delivery-failed"


def test_report_never_raises_when_state_path_is_unwritable():
    action = notify.report("bus-eta-audit", ok=False, detail="d", sink=RecordingSink(),
                           state_path="/proc/definitely/not/writable/s.json", now=NOW)
    assert isinstance(action, str)  # returned rather than raised


# ── the #3 regression: 41 nights, one alert ────────────────────────────────
def test_forty_one_consecutive_failures_ring_exactly_once(tmp_path):
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    actions = [
        notify.report("bus-eta-warehouse", ok=False, detail=f"night {i}",
                      sink=sink, state_path=sp, now=NOW)
        for i in range(41)
    ]
    assert actions[0] == "notified"
    assert set(actions[1:]) == {"suppressed"}
    assert len(sink.sent) == 1


# ── sinks ──────────────────────────────────────────────────────────────────
def test_dry_run_sink_does_not_deliver(capsys):
    notify.DryRunSink().send("routine", "hello")
    assert "hello" in capsys.readouterr().err


def test_telegram_sink_degrades_to_dry_run_without_credentials(capsys):
    # The alerting group does not exist yet; a missing chat_id must not break
    # the two jobs being wired today.
    sink = notify.build_sink(token=None, chat_id=None)
    assert isinstance(sink, notify.DryRunSink)
    assert "dry-run" in capsys.readouterr().err.lower()


def test_build_sink_returns_telegram_when_fully_configured():
    sink = notify.build_sink(token="t", chat_id="c")
    assert isinstance(sink, notify.TelegramSink)


# ── credential must never reach a log file ─────────────────────────────────
def test_telegram_sink_does_not_leak_token_on_http_error():
    """Telegram puts the bot token in the URL path, and httpx's HTTPStatusError
    message embeds the full URL. Printing that exception — which `report()` does
    on delivery failure — would write the token into bus-eta-*.err.log, a file
    that lives for months. One 401 would be a permanent credential leak.
    """
    import httpx

    def handler(request):
        return httpx.Response(401, json={"description": "Unauthorized"})

    sink = notify.TelegramSink("SECRET-TOKEN-123", "chat-1",
                               transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as exc:
        sink.send("routine", "hi")
    assert "SECRET-TOKEN-123" not in str(exc.value)
    assert "401" in str(exc.value)          # still diagnosable


def test_redaction_survives_report_error_path(tmp_path, capsys):
    """End to end: a failing TelegramSink must not put the token on stderr."""
    import httpx

    def handler(request):
        return httpx.Response(401, json={"description": "Unauthorized"})

    sink = notify.TelegramSink("SECRET-TOKEN-123", "chat-1",
                               transport=httpx.MockTransport(handler))
    action = notify.report("bus-eta-audit", ok=False, detail="d", sink=sink,
                           state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "delivery-failed"
    assert "SECRET-TOKEN-123" not in capsys.readouterr().err


def test_redact_replaces_every_occurrence():
    assert "tok" not in notify.redact("a tok b tok c", "tok")


def test_redact_is_a_noop_without_a_secret():
    assert notify.redact("plain message", None) == "plain message"
