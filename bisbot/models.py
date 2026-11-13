"""Plain data objects shared across the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MealItem:
    name: str
    category: str
    portions: float
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "portions": self.portions,
            "confidence": self.confidence,
        }


@dataclass
class ParsedMeal:
    """A meal as understood from the user's message, before or after saving."""

    meal_type: str
    items: list[MealItem] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "meal_type": self.meal_type,
            "items": [item.to_dict() for item in self.items],
            "notes": self.notes,
        }

    def totals(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for item in self.items:
            totals[item.category] = round(totals.get(item.category, 0.0) + item.portions, 2)
        return totals


@dataclass
class StoredMeal:
    """A meal row read back from the database."""

    id: int
    logged_at: datetime
    day: str
    meal: ParsedMeal
    raw_text: str | None = None
    photo_file_id: str | None = None
