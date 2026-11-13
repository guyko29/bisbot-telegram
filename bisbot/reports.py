"""Glue between the database, the summary math and Claude.

Builds the texts for /today, /week and the scheduled summaries, and the context
block Claude gets with every message. Numbers always come from ``summaries``;
Claude only adds a short note, and its failure never blocks a report.

Meals of an "extra" type (the pre-workout snack) are stored like any other
meal but are excluded from every total here.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable

from . import texts
from .claude_client import ClaudeClient, ClaudeError
from .db import Database
from .menu import Menu
from .models import ParsedMeal, StoredMeal
from .summaries import (
    CategoryStatus,
    WeeklyStatus,
    calendar_week_start,
    day_payload,
    day_status,
    format_balance,
    format_day,
    format_patterns,
    format_week,
    format_weekly,
    sum_totals,
    week_days,
    week_payload,
    week_stats,
    weekly_payload,
    weekly_status,
)

logger = logging.getLogger(__name__)

CONTEXT_PREVIOUS_DAYS = 3


class Reports:
    def __init__(self, db: Database, menu: Menu, claude: ClaudeClient):
        self._db = db
        self._menu = menu
        self._claude = claude

    # ---- numbers -------------------------------------------------------

    def _day_statuses(self, day: date) -> list[CategoryStatus]:
        totals = self._db.totals_for_day(day.isoformat(), self._menu.extra_meal_type_keys)
        return day_status(totals, self._menu)

    def _week_so_far(self, day: date) -> list[WeeklyStatus]:
        """Weekly-limited items since Sunday, including today."""
        if not self._menu.weekly_categories:
            return []
        totals = self._db.totals_by_day(
            calendar_week_start(day).isoformat(),
            day.isoformat(),
            self._menu.extra_meal_type_keys,
        )
        return weekly_status(sum_totals(totals), self._menu)

    def _meals_on(self, day: date) -> list[StoredMeal]:
        return self._db.meals_between(day.isoformat(), day.isoformat())

    # ---- deterministic texts -------------------------------------------

    def today_text(self, now: datetime) -> str:
        """/today: eaten vs. target per category and what is left."""
        day = now.date()
        meals = self._meals_on(day)
        parts = [format_day(self._day_statuses(day), day, title="📊 היום")]
        parts += self._extras_and_weekly(day, meals)
        if not meals:
            parts.append(texts.NOTHING_LOGGED_TODAY)
        return self._with_placeholder_warning("\n\n".join(parts))

    def after_save_text(self, now: datetime, meal: ParsedMeal | None = None) -> str:
        """Shown right after a meal is saved or undone: what is left today.

        When the meal used a weekly-limited item, its weekly count is added.
        """
        day = now.date()
        lines = [format_balance(self._day_statuses(day))]
        if meal is not None and not self._menu.is_extra_meal(meal.meal_type):
            touched = {item.category for item in meal.items}
            used = [s for s in self._week_so_far(day) if s.category.key in touched]
            if used:
                lines.append(f"{texts.WEEK_SO_FAR} {format_weekly(used)}")
                if any(s.over for s in used):
                    lines.append(texts.WEEKLY_LIMIT_EXCEEDED)
        return "\n".join(line for line in lines if line)

    def _extras_and_weekly(self, day: date, meals: list[StoredMeal]) -> list[str]:
        parts = []
        extras = [m for m in meals if self._menu.is_extra_meal(m.meal.meal_type)]
        if extras:
            lines = [texts.EXTRAS_TODAY_TITLE]
            for meal in extras:
                items = ", ".join(item.name for item in meal.meal.items)
                lines.append(f"• {self._menu.meal_type_name(meal.meal.meal_type)}: {items}")
            parts.append("\n".join(lines))
        weekly = format_weekly(self._week_so_far(day), only_used=True)
        if weekly:
            parts.append(f"{texts.WEEK_SO_FAR} {weekly}")
        return parts

    # ---- reports with a Claude-written note ----------------------------

    async def daily_report(self, now: datetime) -> str:
        """The 21:00 summary: today's numbers plus a short note from Claude."""
        day = now.date()
        meals = self._meals_on(day)
        statuses = self._day_statuses(day)
        parts = [format_day(statuses, day, title="🌙 סיכום יומי")]
        parts += self._extras_and_weekly(day, meals)
        if not meals:
            parts.append(
                f"{texts.NOTHING_LOGGED_TODAY} אם אכלת ולא תיעדת, אפשר לרשום עכשיו."
            )
            return self._with_placeholder_warning("\n\n".join(parts))
        payload = day_payload(statuses, day)
        payload["meals"] = [self._meal_for_payload(meal, now) for meal in meals]
        payload["weekly_limited_items_since_sunday"] = weekly_payload(self._week_so_far(day))
        try:
            parts.append(await self._claude.phrase_daily(payload))
        except ClaudeError as exc:
            logger.warning("Daily note unavailable: %s", exc)
            parts.append(texts.CLAUDE_NOTE_UNAVAILABLE)
        return self._with_placeholder_warning("\n\n".join(parts))

    async def weekly_report(self, now: datetime) -> str:
        """/week and the Friday summary: the last seven complete days.

        Today is left out because a half-logged day would drag every average
        and adherence figure down.
        """
        days = week_days(now.date() - timedelta(days=1))
        totals = self._db.totals_by_day(
            days[0].isoformat(), days[-1].isoformat(), self._menu.extra_meal_type_keys
        )
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
            "Today so far, per category (computed by the bot, exact; "
            "extra meal types are not included):",
        ]
        for status in self._day_statuses(day):
            category = status.category
            line = f"- {category.key} ({category.name}): eaten {status.eaten:g}"
            if not category.has_daily_target:
                lines.append(f"{line}, no daily target")
                continue
            line += f", target {target_numbers_en(category)}"
            if status.over:
                line += f", over the maximum by {status.over:g}"
            elif status.needed:
                line += f", still needs {status.needed:g}"
                if status.room is not None and status.room > status.needed:
                    line += f" (up to {status.room:g} fits)"
            elif status.room:
                line += f", target met, up to {status.room:g} more fits"
            else:
                line += ", target met"
            lines.append(line)

        weekly = self._week_so_far(day)
        if weekly:
            lines += ["", "Weekly-limited items since Sunday (this week, including today):"]
            lines += [
                f"- {s.category.key} ({s.category.name}): {s.count:g} of at most {s.limit:g}"
                for s in weekly
            ]

        lines += ["", "Meals logged today:"]
        lines += [self._meal_line(meal, now) for meal in self._meals_on(day)] or ["(none yet)"]

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
        extra = " [extra, not in totals]" if self._menu.is_extra_meal(meal.meal.meal_type) else ""
        return f"- {local:%H:%M} {meal.meal.meal_type}{extra}: {items}"

    def _meal_for_payload(self, meal: StoredMeal, now: datetime) -> dict:
        return {
            "time": f"{meal.logged_at.astimezone(now.tzinfo):%H:%M}",
            "meal_type": meal.meal.meal_type,
            "counted_in_daily_totals": not self._menu.is_extra_meal(meal.meal.meal_type),
            "items": [
                {"name": i.name, "category": i.category, "portions": i.portions}
                for i in meal.meal.items
            ],
        }

    def _with_placeholder_warning(self, text: str) -> str:
        if self._menu.placeholder:
            return f"{text}\n\n{texts.PLACEHOLDER_WARNING}"
        return text


def target_numbers_en(category) -> str:
    """'3' / '3 to 5' / 'at least 3' / 'at most 2'."""
    low, high = category.daily_min, category.daily_max
    if low is not None and high is not None:
        return f"{low:g}" if low == high else f"{low:g} to {high:g}"
    if low is not None:
        return f"at least {low:g}"
    return f"at most {high:g}"
