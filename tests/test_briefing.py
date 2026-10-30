"""Tests for the daily morning briefing: prefs, freshness, command and flow."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cmd_briefing import (
    BRIEFING_TRIGGER,
    PELOTON_LOGIN_NOTE,
    cmd_briefing,
    describe_data_freshness,
)
from cmd_llm_common import CommandResult
from cmd_notify import RESET_TARGETS, cmd_notify
from config import (
    BRIEFING_LATE_LIMIT_MIN,
    BRIEFING_MAX_WORDS,
    BRIEFING_RETRY_INTERVAL_MIN,
    DAILY_BRIEFING_TIME_HHMM,
    MAX_REPORT_ATTEMPTS_PER_DAY,
)
from daemon_briefing_flow import TELEGRAM_UNDELIVERED, BriefingFlow
from llm import LLMResult
from llm_verify import VerificationResult, deterministic_verification_issues
from notification_prefs import (
    DEFAULT_NOTIFICATION_PREFS,
    apply_notification_changes,
    daily_briefing_due,
    effective_notification_prefs,
    evaluate_briefing_delivery,
    format_notification_summary,
    format_proposed_changes,
    load_notification_prefs,
    save_notification_prefs,
)

TZ = timezone(timedelta(hours=-6))
FRIDAY = date(2026, 10, 30)
SATURDAY = FRIDAY + timedelta(days=1)


def _at(hhmm: str, day: date = FRIDAY) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def _enabled(time_hhmm: str | None = None) -> dict:
    changes = [{"action": "set", "path": "daily_briefing.enabled", "value": True}]
    if time_hhmm:
        changes.append(
            {"action": "set", "path": "daily_briefing.time", "value": time_hhmm}
        )
    return apply_notification_changes(
        dict(DEFAULT_NOTIFICATION_PREFS), changes, now=_at("00:00")
    )


class TestDailyBriefingPrefs:
    def test_off_by_default_at_the_configured_time(self) -> None:
        section = effective_notification_prefs(dict(DEFAULT_NOTIFICATION_PREFS))[
            "daily_briefing"
        ]

        assert section == {"enabled": False, "time": DAILY_BRIEFING_TIME_HHMM}
        decision = evaluate_briefing_delivery(dict(DEFAULT_NOTIFICATION_PREFS))
        assert decision == {"status": "suppressed", "reason": "disabled"}

    def test_due_from_the_time_until_the_late_limit(self) -> None:
        prefs = _enabled("05:45")
        late_end = _at("05:45") + timedelta(minutes=BRIEFING_LATE_LIMIT_MIN)

        assert not daily_briefing_due(prefs, now=_at("05:44"))
        assert daily_briefing_due(prefs, now=_at("05:45"))
        assert daily_briefing_due(prefs, now=late_end - timedelta(minutes=1))
        assert not daily_briefing_due(prefs, now=late_end)

    def test_custom_time_moves_the_window(self) -> None:
        prefs = _enabled("07:00")

        assert not daily_briefing_due(prefs, now=_at("06:00"))
        assert daily_briefing_due(prefs, now=_at("07:05"))

    def test_mute_suppresses_until_expiry(self) -> None:
        prefs = apply_notification_changes(
            _enabled(),
            [
                {
                    "action": "mute_until",
                    "target": "daily_briefing",
                    "expires_at": _at("23:59").isoformat(),
                    "source_text": "no briefing today",
                }
            ],
            now=_at("05:00"),
        )

        muted = evaluate_briefing_delivery(prefs, now=_at("05:50"))
        assert muted["status"] == "suppressed"
        assert muted["reason"] == "temporary_mute"
        later = evaluate_briefing_delivery(prefs, now=_at("05:50", SATURDAY))
        assert later == {"status": "allowed", "reason": "enabled"}

    def test_invalid_time_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            apply_notification_changes(
                dict(DEFAULT_NOTIFICATION_PREFS),
                [{"action": "set", "path": "daily_briefing.time", "value": "5:45am"}],
            )

    def test_summary_and_proposal_show_the_briefing(self) -> None:
        summary = format_notification_summary(dict(DEFAULT_NOTIFICATION_PREFS))
        assert f"- Daily briefing: daily {DAILY_BRIEFING_TIME_HHMM} (off)" in summary

        proposal = format_proposed_changes(
            dict(DEFAULT_NOTIFICATION_PREFS),
            [{"action": "set", "path": "daily_briefing.enabled", "value": True}],
        )
        assert (
            f"- Daily briefing: daily {DAILY_BRIEFING_TIME_HHMM} (off) -> "
            f"daily {DAILY_BRIEFING_TIME_HHMM} (on)"
        ) in proposal

    def test_override_survives_a_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "notification_prefs.json"
        save_notification_prefs(_enabled("06:10"), path=path)

        loaded = effective_notification_prefs(load_notification_prefs(path))
        assert loaded["daily_briefing"] == {"enabled": True, "time": "06:10"}


class TestNotifyResetTarget:
    def test_daily_briefing_resets_to_default(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "notification_prefs.json"
        save_notification_prefs(_enabled("06:10"), path=path)

        assert "daily_briefing" in RESET_TARGETS
        cmd_notify(
            SimpleNamespace(
                notification_prefs_path=path,
                notify_cmd="reset",
                target="daily_briefing",
            )
        )

        loaded = effective_notification_prefs(load_notification_prefs(path))
        assert loaded["daily_briefing"]["enabled"] is False
        assert "Reset notification settings: daily_briefing." in capsys.readouterr().out


def _insert_daily(conn: sqlite3.Connection, day: date, **values: float) -> None:
    columns = ", ".join(["date", "imported_at", *values])
    marks = ", ".join("?" * (len(values) + 2))
    with conn:
        conn.execute(
            f"INSERT INTO daily ({columns}) VALUES ({marks})",
            (day.isoformat(), f"{day.isoformat()}T05:00:00", *values.values()),
        )


class TestDescribeDataFreshness:
    def test_everything_synced(self, in_memory_db: sqlite3.Connection) -> None:
        _insert_daily(in_memory_db, FRIDAY - timedelta(days=1), sleep_total_h=7.2)
        _insert_daily(in_memory_db, FRIDAY, hrv_ms=48.0, resting_hr=52.0)

        text = describe_data_freshness(in_memory_db, FRIDAY)

        assert "Last night's sleep (night of Thu 29 Oct): synced." in text
        assert "HRV this morning (Fri 30 Oct): synced." in text
        assert "Resting heart rate this morning (Fri 30 Oct): synced." in text
        assert "NOT synced" not in text

    def test_pending_overnight_data_names_the_older_dates(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        _insert_daily(
            in_memory_db,
            FRIDAY - timedelta(days=3),
            sleep_total_h=6.8,
            hrv_ms=44.0,
            resting_hr=53.0,
        )
        # Today's row exists, as an import creates it, but holds no readings.
        _insert_daily(in_memory_db, FRIDAY, steps=120.0)

        text = describe_data_freshness(in_memory_db, FRIDAY)

        assert (
            "Last night's sleep (night of Thu 29 Oct): NOT synced yet; newest "
            "tracked night is Tue 27 Oct."
        ) in text
        assert (
            "HRV this morning: NOT synced yet; newest reading is from Tue 27 Oct."
            in (text)
        )

    def test_manual_sleep_counts(self, in_memory_db: sqlite3.Connection) -> None:
        with in_memory_db:
            in_memory_db.execute(
                "INSERT INTO manual_sleep (date, sleep_total_h, sleep_in_bed_h, "
                "created_at) VALUES (?, ?, ?, ?)",
                ((FRIDAY - timedelta(days=1)).isoformat(), 7.0, 7.5, "2026-10-30"),
            )

        assert (
            "synced." in describe_data_freshness(in_memory_db, FRIDAY).splitlines()[0]
        )

    def test_empty_database(self, in_memory_db: sqlite3.Connection) -> None:
        text = describe_data_freshness(in_memory_db, FRIDAY)

        assert "no sleep has ever synced" in text
        assert "- HRV: no readings have ever synced." in text


class TestVerifierLengthCheck:
    def test_over_the_ceiling_is_flagged(self) -> None:
        draft = " ".join(["word"] * (BRIEFING_MAX_WORDS + 1))

        issues = deterministic_verification_issues("briefing", draft)

        assert any("word ceiling" in issue.problem for issue in issues)

    def test_within_the_ceiling_passes(self) -> None:
        draft = " ".join(["word"] * BRIEFING_MAX_WORDS)

        assert deterministic_verification_issues("briefing", draft) == []

    def test_class_links_do_not_count(self) -> None:
        prose = " ".join(["word"] * (BRIEFING_MAX_WORDS - 5))
        links = "\n".join(
            f"- [30 min Intervals Run {i} — Marcel Maurer](https://x.test/{i})"
            for i in range(5)
        )

        assert deterministic_verification_issues("briefing", f"{prose}\n{links}") == []


def _llm_result(text: str) -> LLMResult:
    return LLMResult(
        text=text,
        model="test-model",
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_s=0.1,
        llm_call_id=7,
    )


def _tool_call_result(name: str) -> LLMResult:
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=name, arguments='{"discipline": "running"}'),
    )
    return LLMResult(
        text="",
        model="test-model",
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_s=0.1,
        llm_call_id=6,
        tool_calls=[call],
        raw_message={
            "role": "assistant",
            "tool_calls": [
                {"id": "call-1", "function": {"name": name, "arguments": "{}"}}
            ],
        },
    )


class TestCmdBriefing:
    build_kwargs: dict

    def _run(
        self,
        in_memory_db: sqlite3.Connection,
        tmp_path: Path,
        *,
        verdict: str = "pass",
        sent_id: int | None = 55,
        telegram: bool = True,
        peloton_tokens_path: Path | None = None,
        search_result: str | None = None,
        briefing_date: date | None = None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[CommandResult, MagicMock]:
        monkeypatch.setattr("cmd_llm_common.ENABLE_LLM_VERIFICATION", True)
        monkeypatch.setattr("cmd_llm_common.VERIFY_BRIEFING", True)
        monkeypatch.setattr(
            "cmd_llm_common.verify_and_rewrite",
            lambda **kwargs: VerificationResult(verdict=verdict, issues=[]),
        )
        args = SimpleNamespace(
            db="ignored.db",
            model="test-model",
            months=1,
            telegram=telegram,
            telegram_id=123,
            reports_dir=tmp_path / "Reports",
            peloton_tokens_path=peloton_tokens_path,
            date=briefing_date,
        )
        self.build_kwargs = {}
        replies = [_llm_result("Easy day.")]
        if search_result is not None:
            replies.insert(0, _tool_call_result("find_peloton_classes"))

            def fake_execute(search, arguments: dict) -> str:
                search.auth_failed = search_result == "auth"
                return '{"error": "describe the class instead."}'

            monkeypatch.setattr("cmd_briefing.PelotonClassSearch.execute", fake_execute)
        with (
            patch(
                "cmd_briefing.load_context",
                return_value={"prompt": "x", "soul": "y", "strategy": "plan"},
            ),
            patch("cmd_briefing.open_db", return_value=in_memory_db),
            patch(
                "cmd_briefing.build_llm_data",
                return_value={
                    "current_week": {"summary": {"week_label": "2026-W44"}, "days": []},
                    "history": [],
                },
            ),
            patch(
                "cmd_briefing.build_messages",
                side_effect=lambda *a, **kw: (
                    self.build_kwargs.update(kw)
                    or [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                    ]
                ),
            ),
            patch("cmd_briefing.call_llm", side_effect=replies) as call,
            patch("tools.run_sql_tool", return_value=[{"type": "function"}]),
            patch("cmd_briefing.date") as fake_date,
            patch("cmd_briefing.send_telegram", return_value=sent_id) as send,
        ):
            fake_date.today.return_value = FRIDAY
            fake_date.fromisoformat = date.fromisoformat
            result = cmd_briefing(args)
            self.last_tools = call.call_args_list[0].kwargs["tools"]
            return result, send

    def test_sends_with_header_and_saves(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        result, send = self._run(in_memory_db, tmp_path, monkeypatch=monkeypatch)

        assert result.text.startswith("**☀️ Morning briefing — Fri 30 Oct**")
        assert result.text.endswith("Easy day.")
        assert result.telegram_message_id == 55
        send.assert_called_once()
        assert send.call_args.kwargs["chat_id"] == "123"
        saved = tmp_path / "Reports" / "briefing_2026-10-30.md"
        assert saved.read_text(encoding="utf-8") == result.text

    def test_verifier_fail_refuses_to_send(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        with pytest.raises(SystemExit):
            self._run(in_memory_db, tmp_path, verdict="fail", monkeypatch=monkeypatch)

        assert not (tmp_path / "Reports").exists()

    def test_telegram_refusal_reports_no_message_id(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        result, _ = self._run(
            in_memory_db, tmp_path, sent_id=None, monkeypatch=monkeypatch
        )

        assert result.telegram_message_id is None
        assert result.text

    def test_no_telegram_prints_without_sending(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        result, send = self._run(
            in_memory_db, tmp_path, telegram=False, monkeypatch=monkeypatch
        )

        send.assert_not_called()
        assert result.telegram_message_id is None

    def test_date_rehearses_another_morning(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        monday = FRIDAY + timedelta(days=3)
        result, _ = self._run(
            in_memory_db,
            tmp_path,
            telegram=False,
            briefing_date=monday,
            monkeypatch=monkeypatch,
        )

        assert result.text.startswith("**☀️ Morning briefing — Mon 2 Nov**")
        assert self.build_kwargs["today"] == monday
        assert (tmp_path / "Reports" / "briefing_2026-11-02.md").exists()

    def test_class_search_offered_only_when_logged_in(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        self._run(in_memory_db, tmp_path, monkeypatch=monkeypatch)
        assert len(self.last_tools) == 1

        tokens = tmp_path / "peloton_tokens.json"
        tokens.write_text("{}", encoding="utf-8")
        self._run(
            in_memory_db,
            tmp_path,
            peloton_tokens_path=tokens,
            monkeypatch=monkeypatch,
        )
        assert self.last_tools[-1]["function"]["name"] == "find_peloton_classes"

    def test_refused_login_adds_a_footer(
        self, in_memory_db, tmp_path, monkeypatch
    ) -> None:
        tokens = tmp_path / "peloton_tokens.json"
        tokens.write_text("{}", encoding="utf-8")
        result, _ = self._run(
            in_memory_db,
            tmp_path,
            peloton_tokens_path=tokens,
            search_result="auth",
            monkeypatch=monkeypatch,
        )

        assert result.text.endswith(PELOTON_LOGIN_NOTE)

    def test_outage_adds_no_footer(self, in_memory_db, tmp_path, monkeypatch) -> None:
        tokens = tmp_path / "peloton_tokens.json"
        tokens.write_text("{}", encoding="utf-8")
        result, _ = self._run(
            in_memory_db,
            tmp_path,
            peloton_tokens_path=tokens,
            search_result="outage",
            monkeypatch=monkeypatch,
        )

        assert result.text.endswith("Easy day.")


def _flow_daemon() -> SimpleNamespace:
    return SimpleNamespace(
        db=Path("health.db"),
        context_dir=Path("ctx"),
        reports_dir=Path("reports"),
        model_prefs_path=None,
        profile=SimpleNamespace(
            telegram_id=1, peloton_tokens=Path("peloton_tokens.json")
        ),
        units="imperial",
        model=None,
        _state={
            "nudge_count_today": 1,
            "nudge_date": FRIDAY.isoformat(),
            "last_nudge_ts": "2026-10-29T18:00:00",
            "recent_nudges": [
                {"ts": "2026-10-29T18:00:00", "trigger": "new_data", "text": "Run."}
            ],
        },
        _save_state=MagicMock(),
        _run_import=MagicMock(),
        _attach_feedback_button=MagicMock(),
        _record_event=MagicMock(),
        _notify_user_failure=MagicMock(),
    )


def _sent(text: str = "**Briefing**\n\nEasy day.") -> CommandResult:
    return CommandResult(text=text, llm_call_id=7, telegram_message_id=99)


class TestBriefingFlow:
    def test_fires_once_per_day(self) -> None:
        daemon = _flow_daemon()
        flow = BriefingFlow(daemon)
        with patch("cmd_briefing.cmd_briefing", return_value=_sent()) as run:
            flow.check(_at("05:46"), _enabled())
            flow.check(_at("05:47"), _enabled())

        run.assert_called_once()
        assert daemon._state["briefing_date"] == FRIDAY.isoformat()
        daemon._attach_feedback_button.assert_called_once()
        assert daemon._attach_feedback_button.call_args.args[1] == BRIEFING_TRIGGER

    def test_not_before_time_nor_after_late_limit_nor_when_disabled(self) -> None:
        daemon = _flow_daemon()
        flow = BriefingFlow(daemon)
        late = _at("05:45") + timedelta(minutes=BRIEFING_LATE_LIMIT_MIN)
        with patch("cmd_briefing.cmd_briefing", return_value=_sent()) as run:
            flow.check(_at("05:30"), _enabled())
            flow.check(late, _enabled())
            flow.check(_at("05:50"), dict(DEFAULT_NOTIFICATION_PREFS))

        run.assert_not_called()

    def test_delivered_briefing_is_shown_to_nudges_without_spending_one(
        self,
    ) -> None:
        daemon = _flow_daemon()
        before = {
            key: daemon._state[key]
            for key in ("nudge_count_today", "nudge_date", "last_nudge_ts")
        }
        with patch("cmd_briefing.cmd_briefing", return_value=_sent("Plan: 30 min.")):
            BriefingFlow(daemon).check(_at("05:46"), _enabled())

        newest = daemon._state["recent_nudges"][0]
        assert newest["trigger"] == BRIEFING_TRIGGER
        assert newest["text"] == "Plan: 30 min."
        assert newest["ts"].startswith("2026-10-30T05:46")
        assert daemon._state["recent_nudges"][1]["trigger"] == "new_data"
        for key, value in before.items():
            assert daemon._state[key] == value
        assert "last_report_ts" not in daemon._state

    def test_transient_failure_retries_after_the_interval_then_gives_up(
        self,
    ) -> None:
        daemon = _flow_daemon()
        flow = BriefingFlow(daemon)
        start = _at("05:45")

        def fail(_args: object) -> CommandResult:
            import logging

            logging.getLogger("llm").error("Connection reset by peer")
            raise SystemExit(1)

        with patch("cmd_briefing.cmd_briefing", side_effect=fail) as run:
            flow.check(start, _enabled())
            flow.check(start + timedelta(minutes=1), _enabled())
            assert run.call_count == 1
            for attempt in range(1, MAX_REPORT_ATTEMPTS_PER_DAY):
                flow.check(
                    start + timedelta(minutes=BRIEFING_RETRY_INTERVAL_MIN * attempt),
                    _enabled(),
                )
            assert run.call_count == MAX_REPORT_ATTEMPTS_PER_DAY
            flow.check(start + timedelta(minutes=90), _enabled())

        assert run.call_count == MAX_REPORT_ATTEMPTS_PER_DAY
        assert daemon._state["briefing_skip_date"] == FRIDAY.isoformat()
        daemon._notify_user_failure.assert_called_once()

    def test_verdict_failure_writes_the_day_off_at_once(self) -> None:
        daemon = _flow_daemon()

        def fail(_args: object) -> CommandResult:
            import logging

            logging.getLogger("cmd_briefing").error(
                "Briefing verification failed; refusing to send it"
            )
            raise SystemExit(1)

        with patch("cmd_briefing.cmd_briefing", side_effect=fail):
            BriefingFlow(daemon).check(_at("05:46"), _enabled())

        assert daemon._state["briefing_skip_date"] == FRIDAY.isoformat()
        daemon._notify_user_failure.assert_called_once()

    def test_undelivered_briefing_is_retried(self) -> None:
        daemon = _flow_daemon()
        undelivered = CommandResult(text="x", llm_call_id=7, telegram_message_id=None)
        with patch("cmd_briefing.cmd_briefing", return_value=undelivered):
            BriefingFlow(daemon).check(_at("05:46"), _enabled())

        assert "briefing_skip_date" not in daemon._state
        assert "briefing_date" not in daemon._state
        assert daemon._state["briefing_attempts"]["count"] == 1
        event = daemon._record_event.call_args
        assert event.args[1] == "retrying"
        assert TELEGRAM_UNDELIVERED[:10] in event.args[3]["error"]

    def test_manual_runs_after_the_scheduled_one_and_reports_failure(self) -> None:
        daemon = _flow_daemon()
        daemon._state["briefing_date"] = FRIDAY.isoformat()
        flow = BriefingFlow(daemon)
        with patch("cmd_briefing.cmd_briefing", return_value=_sent()) as run:
            assert flow.run(manual=True, now=_at("07:00"))
        run.assert_called_once()

        with patch("cmd_briefing.cmd_briefing", side_effect=SystemExit(1)):
            assert not flow.run(manual=True, now=_at("07:05"))
        daemon._notify_user_failure.assert_called_once()
        assert "briefing_attempts" not in daemon._state
