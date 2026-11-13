"""Daily and weekly numbers, computed in Python from DB totals.

Nothing here talks to Claude or Telegram: functions take plain totals
({category: portions}) and return dataclasses, Hebrew text, or JSON-ready
dicts that are later handed to Claude purely for phrasing.

A daily target is a range [daily_min, daily_max]; either bound may be missing
("at least 3", "at most 2") and both are equal for an exact target.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .menu import Category, Menu
from .texts import fmt_day, fmt_num, fmt_qty

EPSILON = 0.01
# A category counts as "consistently" off when it misses the same way on at
# least this share of the logged days (and there are enough days to judge).
CONSISTENT_SHARE = 0.6
MIN_DAYS_FOR_PATTERN = 3


def on_target_tolerance(category: Category) -> float:
    """Slack around an exact target: half a portion, or 10% of it.

    A range ("3 to 5") or a one-sided target already says how much slack the
    plan allows, so those get none.
    """
    if not category.is_exact:
        return 0.0
    return max(0.5, 0.1 * (category.daily_min or 0.0))


def target_numbers(category: Category) -> str:
    """'3' / '3 עד 5' / 'לפחות 3' / 'עד 2' (empty when there is no daily target)."""
    low, high = category.daily_min, category.daily_max
    if low is not None and high is not None:
        return fmt_num(low) if low == high else f"{fmt_num(low)} עד {fmt_num(high)}"
    if low is not None:
        return f"לפחות {fmt_num(low)}"
    if high is not None:
        return f"עד {fmt_num(high)}"
    return ""


def _with_unit(text: str, value: float, category: Category) -> str:
    unit = category.unit_singular if round(value, 2) == 1 else category.unit
    return f"{text} {unit}".strip()


def target_text(category: Category) -> str:
    """The target as it reads after 'מתוך': '6 מנות', '3 עד 5 מנות', '3 מנות לפחות'."""
    low, high = category.daily_min, category.daily_max
    if low is not None and high is not None:
        return _with_unit(target_numbers(category), high, category)
    if low is not None:
        return _with_unit(fmt_num(low), low, category) + " לפחות"
    if high is not None:
        return _with_unit(fmt_num(high), high, category) + " לכל היותר"
    return ""


# ---- one day -----------------------------------------------------------


@dataclass(frozen=True)
class CategoryStatus:
    category: Category
    eaten: float

    @property
    def needed(self) -> float:
        """Portions still missing to reach the daily minimum."""
        if self.category.daily_min is None:
            return 0.0
        return round(max(0.0, self.category.daily_min - self.eaten), 2)

    @property
    def room(self) -> float | None:
        """Portions that still fit under the daily maximum; None when uncapped."""
        if self.category.daily_max is None:
            return None
        return round(max(0.0, self.category.daily_max - self.eaten), 2)

    @property
    def over(self) -> float:
        """Portions eaten beyond the daily maximum."""
        if self.category.daily_max is None:
            return 0.0
        return round(max(0.0, self.eaten - self.category.daily_max), 2)


def day_status(totals: dict[str, float], menu: Menu) -> list[CategoryStatus]:
    """Eaten vs. target for every per-day category, in menu order."""
    return [
        CategoryStatus(category=category, eaten=round(totals.get(category.key, 0.0), 2))
        for category in menu.daily_categories
    ]


def _left_text(status: CategoryStatus) -> str:
    """How much of a category is still open today, without its name."""
    needed, room = status.needed, status.room
    if needed > EPSILON:
        if room is None:
            return f"לפחות {fmt_num(needed)}"
        if room > needed + EPSILON:
            return f"{fmt_num(needed)} עד {fmt_num(room)}"
        return fmt_num(needed)
    if room is not None and room > EPSILON:
        return f"עד {fmt_num(room)}"
    return ""


def format_remaining(statuses: list[CategoryStatus]) -> str:
    """What is still left today: required amounts and optional room."""
    targeted = [s for s in statuses if s.category.has_daily_target]
    if not targeted:
        return ""
    parts = [f"{s.category.name} {_left_text(s)}" for s in targeted if _left_text(s)]
    still_required = any(s.needed > EPSILON for s in targeted)
    if still_required:
        return "נשאר להיום: " + " · ".join(parts)
    done = "🎯 הגעת לכל היעדים להיום!"
    return f"{done}\nאפשר עוד: " + " · ".join(parts) if parts else done


def format_over(statuses: list[CategoryStatus]) -> str:
    """One line listing categories that went past today's maximum."""
    over = [s for s in statuses if s.over > EPSILON]
    if not over:
        return ""
    parts = [f"{s.category.name} {fmt_num(s.over)}" for s in over]
    return "⚠️ מעבר ליעד: " + " · ".join(parts)


def format_balance(statuses: list[CategoryStatus]) -> str:
    """What is left today and what went over, as a few short lines."""
    return "\n".join(line for line in (format_remaining(statuses), format_over(statuses)) if line)


def _day_line(status: CategoryStatus) -> str:
    category = status.category
    if not category.has_daily_target:
        return f"{category.name}: {fmt_qty(status.eaten, category)} (ללא יעד יומי)"
    line = f"{category.name}: {fmt_num(status.eaten)} מתוך {target_text(category)}"
    if status.over > EPSILON:
        return f"{line} · חריגה של {fmt_num(status.over)} ⚠️"
    if status.needed > EPSILON:
        left = _left_text(status).removeprefix("לפחות ")
        return f"{line} · נשארו {left}"
    if status.room is not None and status.room > EPSILON and not category.is_exact:
        return f"{line} ✅ אפשר עוד {fmt_num(status.room)}"
    return f"{line} ✅"


def format_day(statuses: list[CategoryStatus], day: date, *, title: str = "📊 סיכום") -> str:
    lines = [f"{title} · {fmt_day(day)}", ""]
    lines += [_day_line(status) for status in statuses]
    remaining = format_remaining(statuses)
    if remaining:
        lines += ["", remaining]
    return "\n".join(lines)


def day_payload(statuses: list[CategoryStatus], day: date) -> dict[str, Any]:
    """The day's numbers in a JSON-ready shape, for Claude to phrase."""
    return {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "categories": [
            {
                "key": s.category.key,
                "name": s.category.name,
                "eaten": s.eaten,
                "daily_min": s.category.daily_min,
                "daily_max": s.category.daily_max,
                "still_needed_to_reach_min": s.needed,
                "room_left_under_max": s.room,
                "over_max_by": s.over,
            }
            for s in statuses
        ],
    }


# ---- weekly limits -----------------------------------------------------


@dataclass(frozen=True)
class WeeklyStatus:
    category: Category
    count: float

    @property
    def limit(self) -> float:
        return self.category.weekly_limit or 0.0

    @property
    def over(self) -> float:
        return round(max(0.0, self.count - self.limit), 2)


def calendar_week_start(day: date) -> date:
    """The Sunday that starts the week containing `day`."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def sum_totals(totals_by_day: dict[str, dict[str, float]]) -> dict[str, float]:
    summed: dict[str, float] = {}
    for totals in totals_by_day.values():
        for key, value in totals.items():
            summed[key] = round(summed.get(key, 0.0) + value, 2)
    return summed


def weekly_status(totals: dict[str, float], menu: Menu) -> list[WeeklyStatus]:
    """Count vs. weekly limit for every weekly-limited category."""
    return [
        WeeklyStatus(category=category, count=round(totals.get(category.key, 0.0), 2))
        for category in menu.weekly_categories
    ]


def format_weekly(statuses: list[WeeklyStatus], *, only_used: bool = False) -> str:
    """'פיצה 1 מתוך 1 · אלכוהול 3 מתוך 2 ⚠️'."""
    parts = []
    for status in statuses:
        if only_used and status.count <= EPSILON:
            continue
        part = f"{status.category.name} {fmt_num(status.count)} מתוך {fmt_num(status.limit)}"
        parts.append(f"{part} ⚠️" if status.over > EPSILON else part)
    return " · ".join(parts)


def weekly_payload(statuses: list[WeeklyStatus]) -> list[dict[str, Any]]:
    return [
        {
            "key": s.category.key,
            "name": s.category.name,
            "count": s.count,
            "weekly_limit": s.limit,
            "over_limit_by": s.over,
        }
        for s in statuses
    ]


# ---- seven days --------------------------------------------------------


@dataclass(frozen=True)
class WeekCategoryStats:
    category: Category
    total: float
    avg_per_logged_day: float
    adherence_pct: float | None  # None when the category has no daily target
    days_on_target: int = 0
    days_under: int = 0
    days_over: int = 0


@dataclass(frozen=True)
class WeekStats:
    days: list[date]
    logged_days: list[date]
    categories: list[WeekCategoryStats]
    weekly: list[WeeklyStatus] = field(default_factory=list)
    per_day: dict[str, dict[str, float]] = field(default_factory=dict)
    patterns: list[str] = field(default_factory=list)  # Hebrew, human readable

    @property
    def unlogged_days(self) -> list[date]:
        return [d for d in self.days if d not in self.logged_days]

    @property
    def overall_adherence_pct(self) -> float | None:
        scored = [c.adherence_pct for c in self.categories if c.adherence_pct is not None]
        return round(sum(scored) / len(scored), 1) if scored else None


def week_days(last_day: date, length: int = 7) -> list[date]:
    """The `length` consecutive days ending on last_day, oldest first."""
    return [last_day - timedelta(days=offset) for offset in range(length - 1, -1, -1)]


def _adherence_score(eaten: float, category: Category) -> float:
    """1.0 inside the target range, falling linearly to 0 at 100% off a bound."""
    low, high = category.daily_min, category.daily_max
    if low is not None and eaten < low:
        return max(0.0, 1.0 - (low - eaten) / low)
    if high is not None and eaten > high:
        if high <= 0:
            return 0.0
        return max(0.0, 1.0 - (eaten - high) / high)
    return 1.0


def week_stats(
    totals_by_day: dict[str, dict[str, float]], menu: Menu, days: list[date]
) -> WeekStats:
    """Per-category adherence over `days`.

    Days without any logged meal are left out of averages and adherence: no
    log means "unknown", not "ate nothing". They are reported separately.
    """
    logged = [d for d in days if totals_by_day.get(d.isoformat())]
    categories: list[WeekCategoryStats] = []
    for category in menu.daily_categories:
        eaten_per_day = [
            totals_by_day[d.isoformat()].get(category.key, 0.0) for d in logged
        ]
        total = round(sum(eaten_per_day), 2)
        avg = round(total / len(logged), 2) if logged else 0.0
        if not category.has_daily_target or not logged:
            categories.append(WeekCategoryStats(category, total, avg, None))
            continue
        tolerance = on_target_tolerance(category) + EPSILON
        low, high = category.daily_min, category.daily_max
        under = sum(1 for eaten in eaten_per_day if low is not None and eaten < low - tolerance)
        over = sum(1 for eaten in eaten_per_day if high is not None and eaten > high + tolerance)
        scores = [_adherence_score(eaten, category) for eaten in eaten_per_day]
        categories.append(
            WeekCategoryStats(
                category=category,
                total=total,
                avg_per_logged_day=avg,
                adherence_pct=round(100 * sum(scores) / len(scores), 1),
                days_on_target=len(logged) - under - over,
                days_under=under,
                days_over=over,
            )
        )
    per_day = {
        d.isoformat(): {
            key: round(value, 2) for key, value in totals_by_day[d.isoformat()].items()
        }
        for d in logged
    }
    weekly = weekly_status(sum_totals(per_day), menu)
    stats = WeekStats(
        days=days, logged_days=logged, categories=categories, weekly=weekly, per_day=per_day
    )
    return WeekStats(
        days=days,
        logged_days=logged,
        categories=categories,
        weekly=weekly,
        per_day=per_day,
        patterns=_find_patterns(stats),
    )


def _find_patterns(stats: WeekStats) -> list[str]:
    patterns: list[str] = []
    logged = len(stats.logged_days)
    if stats.unlogged_days:
        missing = ", ".join(fmt_day(d) for d in stats.unlogged_days)
        patterns.append(f"ימים בלי תיעוד: {missing}")
    for status in stats.weekly:
        if status.over > EPSILON:
            patterns.append(
                f"{status.category.name}: {fmt_num(status.count)} השבוע, "
                f"מעל המגבלה של {fmt_num(status.limit)}"
            )
    if logged < MIN_DAYS_FOR_PATTERN:
        return patterns

    needed = math.ceil(CONSISTENT_SHARE * logged)
    scored = [c for c in stats.categories if c.adherence_pct is not None]
    for c in scored:
        detail = f"(ממוצע {fmt_num(c.avg_per_logged_day)}, יעד {target_numbers(c.category)})"
        if c.days_under >= needed:
            patterns.append(
                f"{c.category.name}: מתחת ליעד ב-{c.days_under} מתוך {logged} ימים {detail}"
            )
        elif c.days_over >= needed:
            patterns.append(
                f"{c.category.name}: מעל היעד ב-{c.days_over} מתוך {logged} ימים {detail}"
            )
    if len(scored) >= 2:
        best = max(scored, key=lambda c: c.adherence_pct or 0.0)
        worst = min(scored, key=lambda c: c.adherence_pct or 0.0)
        if (best.adherence_pct or 0.0) - (worst.adherence_pct or 0.0) >= 10:
            patterns.append(
                f"הכי קרוב ליעד: {best.category.name} ({round(best.adherence_pct or 0.0)}%), "
                f"הכי רחוק: {worst.category.name} ({round(worst.adherence_pct or 0.0)}%)"
            )
    return patterns


def format_week(stats: WeekStats) -> str:
    first, last = stats.days[0], stats.days[-1]
    lines = [
        f"📅 סיכום שבועי · {first.day}.{first.month} עד {last.day}.{last.month}",
        f"ימים עם תיעוד: {len(stats.logged_days)} מתוך {len(stats.days)}",
    ]
    overall = stats.overall_adherence_pct
    if overall is not None:
        lines.append(f"עמידה כוללת ביעדים: {round(overall)}%")
    lines.append("")
    for c in stats.categories:
        category = c.category
        if c.adherence_pct is None:
            lines.append(
                f"{category.name}: ממוצע {fmt_qty(c.avg_per_logged_day, category)} ליום"
            )
            continue
        lines.append(
            f"{category.name}: ממוצע {fmt_num(c.avg_per_logged_day)} ליום "
            f"(יעד {target_numbers(category)}) · עמידה ביעד {round(c.adherence_pct)}%"
        )
        lines.append(
            f"   ביעד {c.days_on_target} ימים · מתחת {c.days_under} · מעל {c.days_over}"
        )
    if stats.weekly:
        lines += ["", f"מגבלות שבועיות: {format_weekly(stats.weekly)}"]
    return "\n".join(lines)


def format_patterns(stats: WeekStats) -> str:
    """Deterministic fallback for when Claude can't phrase the insights."""
    if not stats.patterns:
        return ""
    return "מה בלט השבוע:\n" + "\n".join(f"• {p}" for p in stats.patterns)


def week_payload(stats: WeekStats) -> dict[str, Any]:
    """The week's numbers in a JSON-ready shape, for Claude to phrase."""
    return {
        "first_day": stats.days[0].isoformat(),
        "last_day": stats.days[-1].isoformat(),
        "days_in_range": len(stats.days),
        "logged_days": [d.isoformat() for d in stats.logged_days],
        "days_without_any_log": [d.isoformat() for d in stats.unlogged_days],
        "overall_adherence_pct": stats.overall_adherence_pct,
        "categories": [
            {
                "key": c.category.key,
                "name": c.category.name,
                "daily_min": c.category.daily_min,
                "daily_max": c.category.daily_max,
                "avg_per_logged_day": c.avg_per_logged_day,
                "adherence_pct": c.adherence_pct,
                "days_on_target": c.days_on_target,
                "days_under_target": c.days_under,
                "days_over_target": c.days_over,
            }
            for c in stats.categories
        ],
        "weekly_limited_items_in_these_7_days": weekly_payload(stats.weekly),
        "portions_per_day": stats.per_day,
        "detected_patterns": stats.patterns,
    }
