"""Glue between the database, the summary math and Claude.

Builds the texts for /today, /week and the scheduled summaries, and the context
block Claude gets with every message. Numbers always come from ``summaries``;
Claude only adds a short note, and its failure never blocks a report.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

from . import texts
from .claude_client import ClaudeClient, ClaudeError
from .db import Database
from .menu import Menu
from .models import StoredMeal
from .summaries import (
    day_payload,
    day_status,
    format_balance,
    format_day,
    format_patterns,
    format_week,
    week_days,
    week_payload,
    week_stats,
)

logger = logging.getLogger(__name__)

CONTEXT_PREVIOUS_DAYS = 3


class Reports:
    def __init__(self, db: Database, menu: Menu, claude: ClaudeClient):
        self._db = db
        self._menu = menu
        self._claude = claude

    # ---- deterministic texts -------------------------------------------

    def today_text(self, now: datetime) -> str:
        """/today: eaten vs. target per category and what is left."""
        day = now.date()
        statuses = day_status(self._db.totals_for_day(day.isoformat()), self._menu)
        text = format_day(statuses, day, title="📊 היום")
        if not self._db.meals_between(day.isoformat(), day.isoformat()):
            text += f"\n\n{texts.NOTHING_LOGGED_TODAY}"
        return self._with_placeholder_warning(text)

    def balance_line(self, day: date) -> str:
        """Short 'what is left today' line shown after a meal is saved."""
        return format_balance(day_status(self._db.totals_for_day(day.isoformat()), self._menu))

    # ---- reports with a Claude-written note ----------------------------

    async def daily_report(self, now: datetime) -> str:
        """The 21:00 summary: today's numbers plus a short note from Claude."""
        day = now.date()
        meals = self._db.meals_between(day.isoformat(), day.isoformat())
        statuses = day_status(self._db.totals_for_day(day.isoformat()), self._menu)
        numbers = format_day(statuses, day, title="🌙 סיכום יומי")
        if not meals:
            return self._with_placeholder_warning(
                f"{numbers}\n\n{texts.NOTHING_LOGGED_TODAY} "
                "אם אכלת ולא תיעדת, אפשר לרשום עכשיו."
            )
        payload = day_payload(statuses, day)
        payload["meals"] = [self._meal_for_payload(meal, now) for meal in meals]
        try:
            note = await self._claude.phrase_daily(payload)
        except ClaudeError as exc:
            logger.warning("Daily note unavailable: %s", exc)
            note = texts.CLAUDE_NOTE_UNAVAILABLE
        return self._with_placeholder_warning(f"{numbers}\n\n{note}")

    async def weekly_report(self, now: datetime) -> str:
        """/week and the Friday summary: the last seven complete days.

        Today is left out because a half-logged day would drag every average
        and adherence figure down.
        """
        days = week_days(now.date() - timedelta(days=1))
        totals = self._db.totals_by_day(days[0].isoformat(), days[-1].isoformat())
        stats = week_stats(totals, self._menu, days)
        if not stats.logged_days:
            return texts.NOTHING_LOGGED_WEEK
        numbers = format_week(stats)
        try:
            note = await self._claude.phrase_weekly(week_payload(stats))
        except ClaudeError as exc:
            logger.warning("Weekly note unavailable: %s", exc)
            note = "\n\n".join(
                part for part in (format_patterns(stats), texts.CLAUDE_NOTE_UNAVAILABLE) if part
            )
        return self._with_placeholder_warning(f"{numbers}\n\n{note}")

    # ---- context for Claude --------------------------------------------

    def claude_context(self, now: datetime, history: Iterable[tuple[str, str]] = ()) -> str:
        """Everything volatile Claude needs to parse a meal or answer a question."""
        day = now.date()
        lines = [
            f"Current local time: {now:%Y-%m-%d %H:%M} ({now:%A}), "
            f"timezone {getattr(now.tzinfo, 'key', now.tzname())}",
            "",
            "Today so far, per category (computed by the bot, exact):",
        ]
        for status in day_status(self._db.totals_for_day(day.isoformat()), self._menu):
            label = f"- {status.category.key} ({status.category.name}): eaten {status.eaten:g}"
            if status.target is None:
                lines.append(f"{label}, no daily target")
            else:
                detail = f"of {status.target:g}, remaining {status.remaining:g}"
                if status.over:
                    detail += f", over by {status.over:g}"
                lines.append(f"{label} {detail}")

        lines += ["", "Meals logged today:"]
        todays = self._db.meals_between(day.isoformat(), day.isoformat())
        lines += [self._meal_line(meal, now) for meal in todays] or ["(none yet)"]

        first = day - timedelta(days=CONTEXT_PREVIOUS_DAYS)
        earlier = self._db.meals_between(first.isoformat(), (day - timedelta(days=1)).isoformat())
        if earlier:
            lines += ["", f"Meals from the previous {CONTEXT_PREVIOUS_DAYS} days:"]
            current_day = None
            for meal in earlier:
                if meal.day != current_day:
                    current_day = meal.day
                    lines.append(f"{meal.day}:")
                lines.append(self._meal_line(meal, now))

        history = list(history)
        if history:
            lines += ["", "Recent conversation (oldest first):"]
            lines += [f"{speaker}: {text}" for speaker, text in history]
        return "\n".join(lines)

    # ---- helpers -------------------------------------------------------

    def _meal_line(self, meal: StoredMeal, now: datetime) -> str:
        local = meal.logged_at.astimezone(now.tzinfo)
        items = ", ".join(
            f"{item.name} ({item.category} {item.portions:g})" for item in meal.meal.items
        )
        return f"- {local:%H:%M} {meal.meal.meal_type}: {items}"

    def _meal_for_payload(self, meal: StoredMeal, now: datetime) -> dict:
        return {
            "time": f"{meal.logged_at.astimezone(now.tzinfo):%H:%M}",
            "meal_type": meal.meal.meal_type,
            "items": [
                {"name": i.name, "category": i.category, "portions": i.portions}
                for i in meal.meal.items
            ],
        }

    def _with_placeholder_warning(self, text: str) -> str:
        if self._menu.placeholder:
            return f"{text}\n\n{texts.PLACEHOLDER_WARNING}"
        return text
