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


# ══════════════════════════════════════════════════════════════════════════
# Verify round 1 fixes (2026-08-13 ensemble, 5 blocking findings)
# ══════════════════════════════════════════════════════════════════════════

class ExitingSink:
    """A sink that raises a BaseException (not an Exception)."""

    def __init__(self, exc):
        self.exc = exc

    def send(self, severity, message):
        raise self.exc


# ── B3: report() must not raise, INCLUDING BaseException ──────────────────
def test_report_survives_systemexit_from_sink(tmp_path):
    """B3. The whole argument of this change is that SystemExit inherits from
    BaseException, not Exception — and `report()` guarded only `Exception`.
    A BaseException from the sink escaped, replacing the caller's original
    SystemExit and destroying the exit code D4 exists to preserve.
    """
    action = notify.report("bus-eta-audit", ok=False, detail="d",
                           sink=ExitingSink(SystemExit(3)),
                           state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "delivery-failed"


def test_report_survives_keyboardinterrupt_from_sink(tmp_path):
    action = notify.report("bus-eta-audit", ok=False, detail="d",
                           sink=ExitingSink(KeyboardInterrupt()),
                           state_path=str(tmp_path / "s.json"), now=NOW)
    assert action == "delivery-failed"


def test_failed_delivery_does_not_consume_the_state_transition(tmp_path):
    """An undelivered alert must be retried next run, not swallowed forever."""
    sp = str(tmp_path / "s.json")
    notify.report("bus-eta-audit", ok=False, detail="d",
                  sink=ExitingSink(SystemExit(3)), state_path=sp, now=NOW)
    sink = RecordingSink()
    assert notify.report("bus-eta-audit", ok=False, detail="d",
                         sink=sink, state_path=sp, now=NOW) == "notified"


# ── B2: dry-run must NOT consume the state transition ─────────────────────
def test_dry_run_does_not_consume_the_state_transition(tmp_path):
    """B2. By design (D5) this module ships in dry-run until the alerting group
    exists. If a dry-run 'send' counts as delivered, every failure during that
    window is marked notified while only reaching the log nobody reads — and
    once Telegram is wired, an ongoing failure is suppressed forever.
    """
    sp = str(tmp_path / "s.json")
    action = notify.report("bus-eta-audit", ok=False, detail="d",
                           sink=notify.DryRunSink(), state_path=sp, now=NOW)
    assert action == "dry-run"
    assert notify.load_state(sp) == {}          # nothing persisted


def test_wiring_telegram_after_a_dry_run_period_still_alerts(tmp_path):
    """The exact deployment sequence: dry-run for N nights, then credentials
    land while the job is still failing. It MUST ring."""
    sp = str(tmp_path / "s.json")
    for _ in range(5):
        notify.report("bus-eta-warehouse", ok=False, detail="d",
                      sink=notify.DryRunSink(), state_path=sp, now=NOW)
    sink = RecordingSink()
    assert notify.report("bus-eta-warehouse", ok=False, detail="d",
                         sink=sink, state_path=sp, now=NOW) == "notified"
    assert len(sink.sent) == 1


def test_real_sink_still_consumes_the_transition():
    """Guard against over-correcting: a delivering sink must still suppress."""
    assert notify.DryRunSink().delivers is False
    assert notify.TelegramSink("t", "c").delivers is True


# ── B1: argparse / clean exits are not job outcomes ───────────────────────
def test_clean_systemexit_is_benign():
    assert notify.is_benign_exit(SystemExit(0)) is True
    assert notify.is_benign_exit(SystemExit(None)) is True


def test_keyboardinterrupt_is_benign():
    assert notify.is_benign_exit(KeyboardInterrupt()) is True


def test_failing_systemexit_is_not_benign():
    assert notify.is_benign_exit(SystemExit(1)) is False
    assert notify.is_benign_exit(SystemExit("volume not mounted")) is False


def test_ordinary_exception_is_not_benign():
    assert notify.is_benign_exit(RuntimeError("boom")) is False


# ══════════════════════════════════════════════════════════════════════════
# report_new — alert-once-per-item (issue #7, verify round 1 B4/B5)
# ══════════════════════════════════════════════════════════════════════════
# audit's outcome is a SET of findings, not a boolean. Projecting it onto
# ok/fail lost information in both directions: a set that emptied because the
# --window-days window slid read as "recovered" (but nothing recovers — TDX
# retains ~2h, holes are permanent), and a set that grew while staying
# non-empty read as "unchanged" (so a disaster tripling in size stayed silent).
#
# In audit's domain there is no such thing as recovery, so the right primitive
# is not a state machine at all: alert once per distinct finding, then be quiet.

ITEM_A = "arrival_event/Taipei/missing/2026-08-01"
ITEM_B = "eta_snapshot/Taipei/missing/2026-08-02"
ITEM_C = "vehicle_position/NewTaipei/low_volume/2026-08-03"


def test_first_sighting_of_an_item_alerts(tmp_path):
    sink = RecordingSink()
    out = notify.report_new("bus-eta-audit", [ITEM_A],
                            sink=sink, state_path=str(tmp_path / "s.json"), now=NOW)
    assert out["status"] == "alerted"
    assert out["alerted"] == [ITEM_A]
    assert len(sink.sent) == 1


def test_same_item_again_is_silent(tmp_path):
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    out = notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    assert out["status"] == "silent"
    assert out["alerted"] == []
    assert len(sink.sent) == 1          # sink untouched the second time


def test_a_new_item_alongside_an_old_one_alerts_only_the_new(tmp_path):
    """B5's core. A disaster getting worse must ring again."""
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    out = notify.report_new("bus-eta-audit", [ITEM_A, ITEM_B], sink=sink, state_path=sp, now=NOW)
    assert out["status"] == "alerted"
    assert out["alerted"] == [ITEM_B]
    assert ITEM_B in sink.sent[1][1] and ITEM_A not in sink.sent[1][1]


def test_items_disappearing_sends_nothing(tmp_path):
    """B4's core. A hole leaving the audit window is NOT a recovery — nothing
    in this system recovers. Silence is correct; announcing health is a lie."""
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    out = notify.report_new("bus-eta-audit", [], sink=sink, state_path=sp, now=NOW)
    assert out["status"] == "silent"
    assert len(sink.sent) == 1
    assert "RECOVERED" not in "".join(m for _, m in sink.sent)


def test_file_count_churn_does_not_create_a_new_item(tmp_path):
    """Risk 1. Item identity is feed/city/kind/date — never the file count,
    which fluctuates daily and would make this ring every night."""
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    out = notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    assert out["alerted"] == []


def test_dry_run_does_not_consume_the_seen_set(tmp_path):
    sp = str(tmp_path / "s.json")
    out = notify.report_new("bus-eta-audit", [ITEM_A],
                            sink=notify.DryRunSink(), state_path=sp, now=NOW)
    assert out["status"] == "dry-run"
    sink = RecordingSink()
    assert notify.report_new("bus-eta-audit", [ITEM_A],
                             sink=sink, state_path=sp, now=NOW)["status"] == "alerted"


def test_failed_delivery_does_not_consume_the_seen_set(tmp_path):
    sp = str(tmp_path / "s.json")
    out = notify.report_new("bus-eta-audit", [ITEM_A],
                            sink=RecordingSink(explode=True), state_path=sp, now=NOW)
    assert out["status"] == "delivery-failed"
    sink = RecordingSink()
    assert notify.report_new("bus-eta-audit", [ITEM_A],
                             sink=sink, state_path=sp, now=NOW)["status"] == "alerted"


def test_report_new_never_raises_on_baseexception_from_sink(tmp_path):
    out = notify.report_new("bus-eta-audit", [ITEM_A],
                            sink=ExitingSink(SystemExit(3)),
                            state_path=str(tmp_path / "s.json"), now=NOW)
    assert out["status"] == "delivery-failed"


def test_prune_before_drops_old_items_without_changing_the_verdict(tmp_path):
    """Risk 2. The seen-set is bounded by the audit window. Safe because the
    window only moves forward — a pruned item cannot re-enter findings."""
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report_new("bus-eta-audit", [ITEM_A, ITEM_C], sink=sink, state_path=sp, now=NOW)
    notify.report_new("bus-eta-audit", [ITEM_C], sink=sink, state_path=sp, now=NOW,
                      prune_before="2026-08-03")
    assert notify.load_state(sp)["bus-eta-audit#seen"] == [ITEM_C]   # A pruned


def test_legacy_state_without_seen_key_reads_as_empty(tmp_path):
    """Risk 5. Old state files just lack the key — first run alerts the current
    window's findings once, then goes quiet."""
    sp = str(tmp_path / "s.json")
    notify.save_state(sp, {"bus-eta-audit": "fail"})
    sink = RecordingSink()
    out = notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    assert out["status"] == "alerted"
    assert notify.load_state(sp)["bus-eta-audit"] == "fail"   # report()'s key untouched


def test_report_new_does_not_disturb_report_state(tmp_path):
    """Family-wide: the two primitives share a file but not a key."""
    sp = str(tmp_path / "s.json")
    sink = RecordingSink()
    notify.report("bus-eta-audit", ok=False, detail="d", sink=sink, state_path=sp, now=NOW)
    notify.report_new("bus-eta-audit", [ITEM_A], sink=sink, state_path=sp, now=NOW)
    st = notify.load_state(sp)
    assert st["bus-eta-audit"] == "fail"
    assert st["bus-eta-audit#seen"] == [ITEM_A]
