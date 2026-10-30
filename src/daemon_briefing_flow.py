"""Daily morning briefing: schedule, retries, and hand-off to nudges.

The briefing goes out at a fixed time each morning (``daily_briefing.time``),
checked every ``BRIEFING_CHECK_INTERVAL_S`` rather than on the half-hourly
report tick, which could land it after the session has started. It sends at
most once a day; a transient failure is retried every
``BRIEFING_RETRY_INTERVAL_MIN`` up to ``MAX_REPORT_ATTEMPTS_PER_DAY`` times,
and only while the morning window (``BRIEFING_LATE_LIMIT_MIN``) is open.

A delivered briefing is added to the daemon's ``recent_nudges`` list, so the
day's nudges see that today's session has been prescribed. It does not spend a
nudge slot or start the nudge spacing interval.

State kept in the daemon's state file: ``briefing_date`` is the day already
briefed, ``briefing_skip_date`` a day written off, and ``briefing_attempts``
today's failed attempts with the time of the last one.
"""

from __future__ import annotations

import logging
import types
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from config import BRIEFING_RETRY_INTERVAL_MIN, MAX_REPORT_ATTEMPTS_PER_DAY
from llm import is_transient_error_text

if TYPE_CHECKING:
    from daemon import ProfileRuntime

logger = logging.getLogger(__name__)

TELEGRAM_UNDELIVERED = "Telegram did not accept the briefing"
"""Failure text for a composed briefing Telegram refused; always retried."""


class BriefingFlow:
    """Runs the scheduled daily briefing and the manual ``/briefing``."""

    def __init__(self, daemon: ProfileRuntime) -> None:
        self._daemon = daemon

    @property
    def _state(self) -> dict[str, Any]:
        return self._daemon._state

    def check(self, now: datetime, prefs: dict[str, Any]) -> None:
        """Run from the briefing tick: send today's briefing if it is due.

        Args:
            now: The current, timezone-aware moment.
            prefs: Loaded notification preferences.
        """
        from notification_prefs import daily_briefing_due, evaluate_briefing_delivery

        if not daily_briefing_due(prefs, now=now):
            return
        if evaluate_briefing_delivery(prefs, now=now)["status"] != "allowed":
            return
        today = now.date().isoformat()
        if today in (
            self._state.get("briefing_date"),
            self._state.get("briefing_skip_date"),
        ):
            return
        attempts = self._state.get("briefing_attempts")
        if isinstance(attempts, dict) and attempts.get("date") == today:
            try:
                last = datetime.fromisoformat(str(attempts.get("last")))
            except ValueError:
                last = None
            if last is not None and now.replace(tzinfo=None) - last < timedelta(
                minutes=BRIEFING_RETRY_INTERVAL_MIN
            ):
                return
        self.run(manual=False, now=now)

    def run(self, *, manual: bool, now: datetime | None = None) -> bool:
        """Compose and send today's briefing.

        Args:
            manual: True for ``/briefing``, which always runs and reports any
                failure at once; False for the scheduled send, which retries
                transient failures on later ticks.
            now: The current, timezone-aware moment; defaults to the clock.
                The once-a-day and retry state are keyed off it, so the
                scheduler's check and this run agree on what today is.

        Returns:
            Whether the briefing was delivered.
        """
        from cmd_briefing import BRIEFING_TRIGGER, cmd_briefing
        from daemon import _capture_last_error

        now = now or datetime.now().astimezone()
        daemon = self._daemon
        daemon._run_import()
        args = types.SimpleNamespace(
            db=str(daemon.db),
            context_dir=daemon.context_dir,
            reports_dir=daemon.reports_dir,
            model_prefs_path=daemon.model_prefs_path,
            telegram_id=(daemon.profile.telegram_id if daemon.profile else None),
            peloton_tokens_path=(
                daemon.profile.peloton_tokens if daemon.profile else None
            ),
            units=daemon.units,
            model=daemon.model,
            telegram=True,
            months=1,
            recent_nudges=self._state.get("recent_nudges", []),
            last_coach_summary=self._state.get("last_coach_summary", ""),
            last_coach_summary_date=self._state.get("last_coach_summary_date", ""),
        )
        with _capture_last_error() as cap:
            try:
                logger.info(
                    "Running daily briefing (%s)", "manual" if manual else "scheduled"
                )
                result = cmd_briefing(args)
            except SystemExit:
                self._failed(
                    manual=manual,
                    now=now,
                    error=cap.last_message,
                    detail=cap.last_suppression,
                )
                return False
        if result.telegram_message_id is None:
            self._failed(
                manual=manual, now=now, error=TELEGRAM_UNDELIVERED, detail=None
            )
            return False

        daemon._attach_feedback_button(result, BRIEFING_TRIGGER)
        self._record_delivered(result.text or "", now)
        daemon._record_event(
            "briefing",
            "fired",
            "Daily briefing sent",
            {"manual": manual},
            llm_call_id=result.llm_call_id,
        )
        return True

    def _record_delivered(self, text: str, now: datetime) -> None:
        """Mark today briefed and show the briefing to the day's nudges.

        Deliberately leaves ``nudge_count_today``, ``last_nudge_ts`` and
        ``last_report_ts`` alone: the briefing is not a nudge and must not
        spend the day's nudge budget or hold the first nudge back.

        Args:
            text: The delivered briefing, header included.
            now: The moment of delivery, timezone-aware.
        """
        from cmd_briefing import BRIEFING_TRIGGER
        from daemon_runners import recent_nudges_within_window

        # Recent nudges are stored as naive local time; keep to that.
        local = now.replace(tzinfo=None)
        self._state["briefing_date"] = now.date().isoformat()
        self._state.pop("briefing_attempts", None)
        recent: list[dict] = list(self._state.get("recent_nudges", []))
        recent.insert(
            0, {"ts": local.isoformat(), "trigger": BRIEFING_TRIGGER, "text": text}
        )
        self._state["recent_nudges"] = recent_nudges_within_window(recent, local)
        self._daemon._save_state()

    def _failed(
        self,
        *,
        manual: bool,
        now: datetime,
        error: str | None,
        detail: object,
    ) -> None:
        """Record a failed attempt and decide whether a later tick retries.

        Args:
            manual: Whether ``/briefing`` asked for this run.
            now: The moment of the attempt, timezone-aware.
            error: Captured error text, if any.
            detail: Verifier suppression snapshot from the run, if any.
        """
        daemon = self._daemon
        if manual:
            daemon._notify_user_failure("Daily briefing", error, detail=detail)
            daemon._record_event(
                "briefing",
                "failed",
                "Manual briefing failed",
                {"manual": True, "error": (error or "")[:500]},
            )
            return

        today = now.date().isoformat()
        record = self._state.get("briefing_attempts")
        if not isinstance(record, dict) or record.get("date") != today:
            record = {"date": today, "count": 0}
        record["count"] = int(record.get("count", 0) or 0) + 1
        record["last"] = now.replace(tzinfo=None).isoformat()
        self._state["briefing_attempts"] = record

        transient = error == TELEGRAM_UNDELIVERED or is_transient_error_text(error)
        retrying = transient and record["count"] < MAX_REPORT_ATTEMPTS_PER_DAY
        if not retrying:
            self._state["briefing_skip_date"] = today
            daemon._notify_user_failure("Daily briefing", error, detail=detail)
        daemon._save_state()
        daemon._record_event(
            "briefing",
            "retrying" if retrying else "failed",
            (
                f"Daily briefing failed, retrying (attempt {record['count']} of "
                f"{MAX_REPORT_ATTEMPTS_PER_DAY})"
                if retrying
                else "Daily briefing failed"
            ),
            {
                "error": (error or "")[:500],
                "attempt": record["count"],
                "transient": transient,
            },
        )
