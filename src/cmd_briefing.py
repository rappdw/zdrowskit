"""Daily morning briefing: readiness, today's session and a class to look for.

Sent at a fixed time before the user trains, so it routinely runs before last
night's sleep or this morning's HRV has synced. The freshness of each overnight
reading is computed here, from the database, and handed to the writer and the
verifier as fact: a briefing may work from an older reading, but it has to say
which day that reading is from.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from baselines import unestablished_metrics
from charts import strip_charts
from cmd_llm_common import (
    CommandResult,
    apply_verification,
    route_kwargs,
    telegram_chat_id,
)
from config import (
    CONTEXT_DIR,
    MAX_TOKENS_BRIEFING,
    MAX_TOOL_ITERATIONS_BRIEFING,
    METRIC_TRUST_WINDOW_DAYS,
    REPORTS_DIR,
)
from data_maturity import build_data_maturity
from llm import call_llm
from llm_context import build_messages, load_context, load_prompt_text
from llm_health import (
    format_last_coach_summary,
    format_recent_nudges,
    render_health_data,
)
from llm_health_data import build_llm_data
from llm_verify import extract_tool_evidence, slim_source_messages
from notify import send_telegram
from peloton import PelotonClassSearch, find_classes_tool, has_tokens
from store import create_llm_trace, open_db
from units import units_from_args

logger = logging.getLogger(__name__)

BRIEFING_TRIGGER = "briefing"
"""Trigger label the briefing carries in the daemon's recent-nudges list."""

_TOOL_FOLLOWUP = load_prompt_text("briefing_tool_followup")

PELOTON_LOGIN_NOTE = (
    "Peloton class search is unavailable: the stored login was refused. "
    "Run `peloton login` to restore it."
)
"""Footer added when the class search failed for want of a valid login."""


def _day_label(day: date) -> str:
    """Return ``Thu 29 Oct`` for a date."""
    return f"{day:%a} {day.day} {day:%b}"


def _latest_on_or_before(
    conn: sqlite3.Connection, table: str, column: str, through: str
) -> str | None:
    """Return the newest date on or before ``through`` holding ``column``."""
    row = conn.execute(
        f"SELECT MAX(date) FROM {table} WHERE date <= ? AND {column} IS NOT NULL",
        (through,),
    ).fetchone()
    return row[0] if row and row[0] else None


def describe_data_freshness(conn: sqlite3.Connection, today: date) -> str:
    """Say which overnight readings the briefing can treat as current.

    Sleep is stored under the night it started, so last night is yesterday's
    date. HRV and resting heart rate are stored under the day they were
    measured, so this morning's readings are today's date. Each line names the
    date of the newest reading when the current one is missing, so the writer
    can use it honestly instead of passing it off as fresh.

    Args:
        conn: Open database connection.
        today: The local date the briefing is for.

    Returns:
        A short markdown list, one line per reading.
    """
    night = today - timedelta(days=1)
    lines: list[str] = []

    latest_sleep = _latest_on_or_before(
        conn, "sleep_all", "sleep_total_h", night.isoformat()
    )
    if latest_sleep == night.isoformat():
        lines.append(f"- Last night's sleep (night of {_day_label(night)}): synced.")
    else:
        older = (
            f"newest tracked night is {_day_label(date.fromisoformat(latest_sleep))}"
            if latest_sleep
            else "no sleep has ever synced"
        )
        lines.append(
            f"- Last night's sleep (night of {_day_label(night)}): NOT synced "
            f"yet; {older}."
        )

    for column, label in (("hrv_ms", "HRV"), ("resting_hr", "Resting heart rate")):
        latest = _latest_on_or_before(conn, "daily", column, today.isoformat())
        if latest == today.isoformat():
            lines.append(f"- {label} this morning ({_day_label(today)}): synced.")
        elif latest:
            lines.append(
                f"- {label} this morning: NOT synced yet; newest reading is from "
                f"{_day_label(date.fromisoformat(latest))}."
            )
        else:
            lines.append(f"- {label}: no readings have ever synced.")
    return "\n".join(lines)


def briefing_header(today: date) -> str:
    """Return the bold header line the app puts above the briefing."""
    return f"**☀️ Morning briefing — {_day_label(today)}**"


def _save_briefing(text: str, today: date, reports_dir: Path) -> Path:
    """Write the briefing to ``briefing_YYYY-MM-DD.md`` in the reports dir."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"briefing_{today.isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    logger.info("Briefing saved to %s", path)
    return path


def cmd_briefing(
    args: argparse.Namespace,
    reply_markup: dict | None = None,
) -> CommandResult:
    """Handle the 'briefing' subcommand: compose and send today's briefing.

    Args:
        args: Parsed CLI arguments with db, model, months, telegram and the
            profile paths, including peloton_tokens_path, whose presence turns
            on the Peloton class search; the daemon also passes recent_nudges
            and the last coach summary.
        reply_markup: Optional Telegram reply markup for the message.

    Returns:
        A CommandResult. ``telegram_message_id`` is None when sending was
        requested and Telegram did not accept the message.
    """
    try:
        context_dir = Path(getattr(args, "context_dir", CONTEXT_DIR))
        context = load_context(
            context_dir, prompt_file="briefing_prompt", max_history=3, max_log=5
        )
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    today = date.today()
    conn = open_db(Path(args.db))
    units = units_from_args(args)
    health_data = build_llm_data(conn, getattr(args, "months", 1))
    health_data_text = render_health_data(
        health_data,
        prompt_kind="nudge",
        unestablished=unestablished_metrics(conn, METRIC_TRUST_WINDOW_DAYS),
        units=units,
    )
    freshness = describe_data_freshness(conn, today)
    context["data_freshness"] = freshness
    context["recent_nudges"] = format_recent_nudges(
        getattr(args, "recent_nudges", []), empty_text="(none yet)"
    )
    context["last_coach_summary"] = format_last_coach_summary(
        getattr(args, "last_coach_summary", ""),
        getattr(args, "last_coach_summary_date", ""),
    )
    tokens_path = getattr(args, "peloton_tokens_path", None)
    class_search = PelotonClassSearch(tokens_path) if has_tokens(tokens_path) else None
    context["class_guidance"] = load_prompt_text(
        "briefing_class_search" if class_search else "briefing_class_spec"
    )

    trace_id = create_llm_trace(conn, "briefing")
    messages = build_messages(
        context,
        health_data_text,
        data_maturity=build_data_maturity(conn, context),
        units=units,
    )

    from tools import execute_run_sql, run_sql_tool

    tools = run_sql_tool() + (find_classes_tool() if class_search else [])

    # The briefing reuses the nudge route: same latency budget and the same
    # kind of judgment, so a separate routing knob would only be one more
    # setting to keep in step.
    route = route_kwargs(
        "nudge",
        getattr(args, "model", None),
        prefs_path=getattr(args, "model_prefs_path", None),
    )
    call_kwargs = {
        "model": route["model"],
        "max_tokens": MAX_TOKENS_BRIEFING,
        "temperature": route.get("temperature", 0.7),
        "reasoning_effort": route.get("reasoning_effort"),
        "fallback_models": route.get("fallback_models"),
        "conn": conn,
        "request_type": "briefing",
        "trace_id": trace_id,
    }
    logger.info("Calling %s for the daily briefing ...", route["model"])

    result = None
    for iteration in range(MAX_TOOL_ITERATIONS_BRIEFING):
        try:
            result = call_llm(
                messages,
                tools=tools,
                metadata={"iteration": iteration},
                **call_kwargs,
            )
        except Exception as e:
            logger.error("Briefing LLM call failed: %s: %s", type(e).__name__, e)
            sys.exit(1)
        if not result.tool_calls:
            break
        messages.append(result.raw_message)
        for tc in result.tool_calls:
            try:
                tool_args = (
                    json.loads(tc.function.arguments)
                    if isinstance(tc.function.arguments, str)
                    else tc.function.arguments
                )
            except (ValueError, json.JSONDecodeError):
                tool_args = {}
            if tc.function.name == "run_sql":
                logger.info("Briefing SQL: %s", tool_args.get("query", "")[:200])
                tool_result = execute_run_sql(Path(args.db), tool_args)
            elif tc.function.name == "find_peloton_classes" and class_search:
                logger.info("Briefing Peloton search: %s", tool_args)
                tool_result = class_search.execute(tool_args)
            else:
                tool_result = json.dumps({"error": f"Unknown tool: {tc.function.name}"})
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
            )
        messages.append({"role": "user", "content": _TOOL_FOLLOWUP})

    if result is not None and result.tool_calls and not (result.text or "").strip():
        # The loop ran out with the model still asking for tools: answer the
        # pending calls with the budget notice and force one tool-less answer.
        logger.warning("Briefing tool budget exhausted; forcing final synthesis")
        messages.append(result.raw_message)
        budget_notice = json.dumps({"error": load_prompt_text("tool_budget_briefing")})
        for tc in result.tool_calls:
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": budget_notice}
            )
        try:
            result = call_llm(
                messages,
                tools=None,
                metadata={"iteration": "final_synthesis"},
                **call_kwargs,
            )
        except Exception as e:
            logger.error("Briefing final synthesis call failed: %s", e)
            sys.exit(1)

    raw_text = (result.text if result else "").strip()
    if not raw_text:
        logger.error("Briefing came back empty; nothing to send")
        sys.exit(1)

    verified = apply_verification(
        kind="briefing",
        draft=raw_text,
        evidence={
            "health_data_text": health_data_text,
            "data_freshness": freshness,
            "strategy": context.get("strategy"),
            "about_user": context.get("me"),
            "recent_nudges_text": context.get("recent_nudges"),
            "last_coach_summary": context.get("last_coach_summary"),
            "display_units": str(units),
            "class_search": "peloton" if class_search else "unavailable",
            "tool_calls": extract_tool_evidence(messages),
        },
        source_messages=slim_source_messages(messages, raw_text),
        conn=conn,
        metadata={"source_llm_call_id": result.llm_call_id},
        trace_id=trace_id,
        model_prefs_path=getattr(args, "model_prefs_path", None),
    )
    if verified is None:
        logger.error("Briefing verification failed; refusing to send it")
        sys.exit(1)

    text = f"{briefing_header(today)}\n\n{strip_charts(verified.strip()).strip()}"
    if class_search and class_search.auth_failed:
        text = f"{text}\n\n{PELOTON_LOGIN_NOTE}"
    _save_briefing(text, today, Path(getattr(args, "reports_dir", REPORTS_DIR)))
    print(text)

    telegram_message_id: int | None = None
    if getattr(args, "telegram", True):
        telegram_message_id = send_telegram(
            text,
            "zdrowskit — morning briefing",
            reply_markup,
            chat_id=telegram_chat_id(args),
        )
    return CommandResult(
        text=text,
        llm_call_id=result.llm_call_id,
        telegram_message_id=telegram_message_id,
    )
