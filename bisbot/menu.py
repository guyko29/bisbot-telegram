"""The meal plan: free text for Claude plus machine-readable categories/targets.

``menu.md`` is the nutritionist's plan as written; it goes into Claude's system
prompt verbatim. ``menu_targets.json`` lists the portion categories from that
plan with their daily targets, so totals are computed in Python and never by
the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class MenuError(RuntimeError):
    """Raised when the menu files are missing or malformed."""


@dataclass(frozen=True)
class Category:
    key: str
    name: str  # Hebrew display name
    unit: str = "מנות"  # plural unit label
    unit_singular: str = "מנה"
    daily_target: float | None = None  # None = tracked, but no daily target


@dataclass(frozen=True)
class MealType:
    key: str
    name: str  # Hebrew display name


DEFAULT_MEAL_TYPES: tuple[MealType, ...] = (
    MealType("breakfast", "ארוחת בוקר"),
    MealType("snack", "ארוחת ביניים"),
    MealType("lunch", "ארוחת צהריים"),
    MealType("dinner", "ארוחת ערב"),
    MealType("pre_workout", "לפני אימון"),
    MealType("post_workout", "אחרי אימון"),
    MealType("other", "אחר"),
)


@dataclass(frozen=True)
class Menu:
    text: str
    categories: tuple[Category, ...]
    meal_types: tuple[MealType, ...] = DEFAULT_MEAL_TYPES
    placeholder: bool = False

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
        target = entry.get("daily_target")
        if target is not None:
            if isinstance(target, bool) or not isinstance(target, (int, float)):
                raise MenuError(f"menu targets: daily_target of '{key}' must be a number")
            if target < 0:
                raise MenuError(f"menu targets: daily_target of '{key}' is negative")
            target = float(target)
        categories.append(
            Category(
                key=key,
                name=name,
                unit=str(entry.get("unit", "מנות")),
                unit_singular=str(entry.get("unit_singular", "מנה")),
                daily_target=target,
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
        meal_types.append(MealType(str(entry["key"]), str(entry["name"])))
    if len({m.key for m in meal_types}) != len(meal_types):
        raise MenuError("menu targets: duplicate meal type key")
    return tuple(meal_types)


def parse_targets(data: object) -> tuple[tuple[Category, ...], tuple[MealType, ...], bool]:
    if not isinstance(data, dict):
        raise MenuError("menu targets: top level must be a JSON object")
    return (
        _parse_categories(data.get("categories")),
        _parse_meal_types(data.get("meal_types")),
        bool(data.get("placeholder", False)),
    )


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
    categories, meal_types, placeholder = parse_targets(data)
    return Menu(text=text, categories=categories, meal_types=meal_types, placeholder=placeholder)
