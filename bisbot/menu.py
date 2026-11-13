"""The meal plan: free text for Claude plus machine-readable categories/targets.

``menu.md`` is the nutritionist's plan as written; it goes into Claude's system
prompt verbatim. ``menu_targets.json`` lists the portion categories derived
from that plan with their daily targets and weekly limits, so totals are
computed in Python and never by the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class MenuError(RuntimeError):
    """Raised when the menu files are missing or malformed."""


@dataclass(frozen=True)
class Category:
    """A portion category the bot keeps totals for.

    Daily targets are a range: both bounds for "3 to 5", equal bounds for an
    exact target, only ``daily_min`` for "at least", only ``daily_max`` for "at
    most". ``weekly_limit`` marks things the plan allows a few times per week.
    """

    key: str
    name: str  # Hebrew display name
    unit: str = "מנות"  # plural unit label; may be empty
    unit_singular: str = "מנה"
    daily_min: float | None = None
    daily_max: float | None = None
    weekly_limit: float | None = None

    @property
    def has_daily_target(self) -> bool:
        return self.daily_min is not None or self.daily_max is not None

    @property
    def is_weekly(self) -> bool:
        return self.weekly_limit is not None

    @property
    def is_exact(self) -> bool:
        return self.daily_min is not None and self.daily_min == self.daily_max


@dataclass(frozen=True)
class MealType:
    key: str
    name: str  # Hebrew display name
    # Extra meals (e.g. the pre-workout snack) are logged but kept out of the
    # daily and weekly totals.
    extra: bool = False


DEFAULT_MEAL_TYPES: tuple[MealType, ...] = (
    MealType("breakfast", "ארוחת בוקר"),
    MealType("snack", "ארוחת ביניים"),
    MealType("lunch", "ארוחת צהריים"),
    MealType("dinner", "ארוחת ערב"),
    MealType("pre_workout", "לפני אימון", extra=True),
    MealType("other", "אחר"),
)


@dataclass(frozen=True)
class Menu:
    text: str
    categories: tuple[Category, ...]
    meal_types: tuple[MealType, ...] = DEFAULT_MEAL_TYPES
    placeholder: bool = False
    # Plan-specific instructions for Claude on turning food into portions.
    counting_rules: str = ""

    @property
    def daily_categories(self) -> tuple[Category, ...]:
        """Categories shown in the per-day view (everything without a weekly limit)."""
        return tuple(c for c in self.categories if not c.is_weekly)

    @property
    def weekly_categories(self) -> tuple[Category, ...]:
        return tuple(c for c in self.categories if c.is_weekly)

    @property
    def extra_meal_type_keys(self) -> list[str]:
        return [m.key for m in self.meal_types if m.extra]

    def is_extra_meal(self, meal_type: str) -> bool:
        return meal_type in self.extra_meal_type_keys

    @property
    def category_keys(self) -> list[str]:
        return [c.key for c in self.categories]

    @property
    def meal_type_keys(self) -> list[str]:
        return [m.key for m in self.meal_types]

    def category(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)

    def meal_type_name(self, key: str) -> str:
        return next((m.name for m in self.meal_types if m.key == key), key)


def _number(entry: dict, field: str, key: str) -> float | None:
    value = entry.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MenuError(f"menu targets: {field} of '{key}' must be a number")
    if value < 0:
        raise MenuError(f"menu targets: {field} of '{key}' is negative")
    return float(value)


def _parse_categories(raw: object) -> tuple[Category, ...]:
    if not isinstance(raw, list) or not raw:
        raise MenuError("menu targets: 'categories' must be a non-empty list")
    categories: list[Category] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise MenuError("menu targets: every category must be an object")
        key = str(entry.get("key", "")).strip()
        name = str(entry.get("name", "")).strip()
        if not key or not name:
            raise MenuError("menu targets: every category needs 'key' and 'name'")
        if key in seen:
            raise MenuError(f"menu targets: duplicate category key '{key}'")
        seen.add(key)
        daily_min = _number(entry, "daily_min", key)
        daily_max = _number(entry, "daily_max", key)
        target = _number(entry, "daily_target", key)
        if target is not None:
            if daily_min is not None or daily_max is not None:
                raise MenuError(
                    f"menu targets: '{key}' has daily_target together with daily_min/daily_max"
                )
            daily_min = daily_max = target
        if daily_min is not None and daily_max is not None and daily_min > daily_max:
            raise MenuError(f"menu targets: daily_min of '{key}' is above its daily_max")
        weekly_limit = _number(entry, "weekly_limit", key)
        if weekly_limit is not None and (daily_min is not None or daily_max is not None):
            raise MenuError(
                f"menu targets: '{key}' cannot have both a weekly limit and a daily target"
            )
        categories.append(
            Category(
                key=key,
                name=name,
                unit=str(entry.get("unit", "מנות")),
                unit_singular=str(entry.get("unit_singular", "מנה")),
                daily_min=daily_min,
                daily_max=daily_max,
                weekly_limit=weekly_limit,
            )
        )
    return tuple(categories)


def _parse_meal_types(raw: object) -> tuple[MealType, ...]:
    if raw is None:
        return DEFAULT_MEAL_TYPES
    if not isinstance(raw, list) or not raw:
        raise MenuError("menu targets: 'meal_types' must be a non-empty list")
    meal_types: list[MealType] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("key") or not entry.get("name"):
            raise MenuError("menu targets: every meal type needs 'key' and 'name'")
        meal_types.append(
            MealType(str(entry["key"]), str(entry["name"]), extra=bool(entry.get("extra", False)))
        )
    if len({m.key for m in meal_types}) != len(meal_types):
        raise MenuError("menu targets: duplicate meal type key")
    return tuple(meal_types)


def _parse_counting_rules(raw: object) -> str:
    """Accepts one string or a list of paragraphs (easier to edit in JSON)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list) and all(isinstance(part, str) for part in raw):
        return "\n\n".join(part.strip() for part in raw if part.strip())
    raise MenuError("menu targets: 'counting_rules' must be a string or a list of strings")


def parse_targets(data: object) -> dict:
    """Validated keyword arguments for Menu (everything except the plan text)."""
    if not isinstance(data, dict):
        raise MenuError("menu targets: top level must be a JSON object")
    return {
        "categories": _parse_categories(data.get("categories")),
        "meal_types": _parse_meal_types(data.get("meal_types")),
        "placeholder": bool(data.get("placeholder", False)),
        "counting_rules": _parse_counting_rules(data.get("counting_rules")),
    }


def load_menu(menu_path: Path, targets_path: Path) -> Menu:
    try:
        text = menu_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MenuError(f"Cannot read menu file {menu_path}: {exc}") from exc
    if not text:
        raise MenuError(f"Menu file {menu_path} is empty")
    try:
        data = json.loads(targets_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MenuError(f"Cannot read targets file {targets_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MenuError(f"Targets file {targets_path} is not valid JSON: {exc}") from exc
    return Menu(text=text, **parse_targets(data))
