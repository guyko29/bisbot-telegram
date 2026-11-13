"""Validation of meal payloads (from Claude's tool call or stored shortcuts)."""

from __future__ import annotations

import math
from typing import Any

from .menu import Menu
from .models import MealItem, ParsedMeal

MAX_PORTIONS_PER_ITEM = 20.0
MAX_ITEMS = 30


class MealValidationError(ValueError):
    """The payload does not describe a meal the bot can store."""


def _as_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise MealValidationError(f"{field} must be a number")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise MealValidationError(f"{field} must be a number") from exc
    else:
        raise MealValidationError(f"{field} must be a number")
    if not math.isfinite(number):
        raise MealValidationError(f"{field} must be a finite number")
    return number


def validate_meal(payload: Any, menu: Menu) -> ParsedMeal:
    """Turn an untrusted dict into a ParsedMeal, or raise MealValidationError.

    Categories are checked against the menu because every total and target is
    keyed by them; an unknown category would silently vanish from summaries.
    """
    if not isinstance(payload, dict):
        raise MealValidationError("meal must be an object")

    meal_type = str(payload.get("meal_type") or "").strip()
    if meal_type not in menu.meal_type_keys:
        if "other" not in menu.meal_type_keys:
            raise MealValidationError(f"unknown meal_type '{meal_type}'")
        meal_type = "other"

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise MealValidationError("items must be a non-empty list")
    if len(raw_items) > MAX_ITEMS:
        raise MealValidationError(f"too many items (max {MAX_ITEMS})")

    items: list[MealItem] = []
    for index, raw in enumerate(raw_items):
        where = f"items[{index}]"
        if not isinstance(raw, dict):
            raise MealValidationError(f"{where} must be an object")

        name = str(raw.get("name") or "").strip()
        if not name:
            raise MealValidationError(f"{where}.name is empty")

        category = str(raw.get("category") or "").strip()
        if category not in menu.category_keys:
            allowed = ", ".join(menu.category_keys)
            raise MealValidationError(
                f"{where}.category '{category}' is not one of: {allowed}"
            )

        portions = round(_as_number(raw.get("portions"), f"{where}.portions"), 2)
        if portions <= 0:
            raise MealValidationError(f"{where}.portions must be greater than 0")
        if portions > MAX_PORTIONS_PER_ITEM:
            raise MealValidationError(
                f"{where}.portions is implausibly large (max {MAX_PORTIONS_PER_ITEM:g})"
            )

        confidence_raw = raw.get("confidence", 1.0)
        confidence = 1.0 if confidence_raw is None else _as_number(
            confidence_raw, f"{where}.confidence"
        )
        confidence = min(1.0, max(0.0, confidence))

        items.append(
            MealItem(name=name, category=category, portions=portions, confidence=confidence)
        )

    notes = payload.get("notes")
    return ParsedMeal(
        meal_type=meal_type,
        items=items,
        notes=notes.strip() if isinstance(notes, str) else "",
    )
