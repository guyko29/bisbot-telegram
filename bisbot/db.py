"""Thin SQLite data-access layer.

Everything the rest of the bot needs from storage goes through ``Database``,
so swapping SQLite for another backend means reimplementing this one class.

Timestamps are stored in UTC; ``meals.day`` holds the local calendar date
(YYYY-MM-DD in the configured timezone) so per-day sums need no timezone math.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import MealItem, ParsedMeal, StoredMeal

SCHEMA = """
CREATE TABLE IF NOT EXISTS meals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at     TEXT NOT NULL,
    day           TEXT NOT NULL,
    meal_type     TEXT NOT NULL,
    raw_text      TEXT,
    photo_file_id TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_meals_day ON meals (day, status);

CREATE TABLE IF NOT EXISTS items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id  INTEGER NOT NULL REFERENCES meals (id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    portions REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_meal ON items (meal_id);

CREATE TABLE IF NOT EXISTS shortcuts (
    name       TEXT PRIMARY KEY COLLATE NOCASE,
    items_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending (
    chat_id      INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL
);
"""

CONFIRMED = "confirmed"
DELETED = "deleted"


def normalize_shortcut_name(name: str) -> str:
    """Collapse whitespace so 'my   breakfast ' and 'my breakfast' are one name."""
    return " ".join(name.split())


class Database:
    def __init__(self, path: Path | str):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- meals ---------------------------------------------------------

    def add_meal(
        self,
        meal: ParsedMeal,
        *,
        logged_at: datetime,
        day: str,
        raw_text: str | None = None,
        photo_file_id: str | None = None,
    ) -> int:
        """Store a confirmed meal with its items; returns the meal id."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO meals (logged_at, day, meal_type, raw_text, photo_file_id, notes, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    logged_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    day,
                    meal.meal_type,
                    raw_text,
                    photo_file_id,
                    meal.notes,
                    CONFIRMED,
                ),
            )
            meal_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO items (meal_id, name, category, portions) VALUES (?, ?, ?, ?)",
                [(meal_id, i.name, i.category, i.portions) for i in meal.items],
            )
        return meal_id

    def last_meal(self) -> StoredMeal | None:
        """The most recently logged meal that has not been undone."""
        row = self._conn.execute(
            "SELECT * FROM meals WHERE status = ? ORDER BY id DESC LIMIT 1", (CONFIRMED,)
        ).fetchone()
        return self._to_stored(row) if row else None

    def delete_meal(self, meal_id: int) -> bool:
        """Soft-delete: the row stays for auditing but leaves every total."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE meals SET status = ? WHERE id = ? AND status = ?",
                (DELETED, meal_id, CONFIRMED),
            )
        return cursor.rowcount > 0

    def meals_between(self, start_day: str, end_day: str) -> list[StoredMeal]:
        """Confirmed meals with start_day <= day <= end_day, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM meals WHERE status = ? AND day BETWEEN ? AND ?"
            " ORDER BY day, logged_at, id",
            (CONFIRMED, start_day, end_day),
        ).fetchall()
        return [self._to_stored(row) for row in rows]

    def totals_for_day(self, day: str) -> dict[str, float]:
        return self.totals_by_day(day, day).get(day, {})

    def totals_by_day(self, start_day: str, end_day: str) -> dict[str, dict[str, float]]:
        """{day: {category: portions}} for days that have at least one meal."""
        rows = self._conn.execute(
            "SELECT m.day AS day, i.category AS category, SUM(i.portions) AS portions"
            " FROM items i JOIN meals m ON m.id = i.meal_id"
            " WHERE m.status = ? AND m.day BETWEEN ? AND ?"
            " GROUP BY m.day, i.category",
            (CONFIRMED, start_day, end_day),
        ).fetchall()
        totals: dict[str, dict[str, float]] = {}
        for row in rows:
            totals.setdefault(row["day"], {})[row["category"]] = round(row["portions"], 2)
        return totals

    def _to_stored(self, row: sqlite3.Row) -> StoredMeal:
        item_rows = self._conn.execute(
            "SELECT name, category, portions FROM items WHERE meal_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        meal = ParsedMeal(
            meal_type=row["meal_type"],
            items=[MealItem(r["name"], r["category"], r["portions"]) for r in item_rows],
            notes=row["notes"] or "",
        )
        return StoredMeal(
            id=row["id"],
            logged_at=datetime.fromisoformat(row["logged_at"]),
            day=row["day"],
            meal=meal,
            raw_text=row["raw_text"],
            photo_file_id=row["photo_file_id"],
        )

    # ---- shortcuts -----------------------------------------------------

    def save_shortcut(self, name: str, meal: ParsedMeal) -> None:
        payload = {
            "meal_type": meal.meal_type,
            "items": [
                {"name": i.name, "category": i.category, "portions": i.portions}
                for i in meal.items
            ],
        }
        with self._conn:
            self._conn.execute(
                "INSERT INTO shortcuts (name, items_json) VALUES (?, ?)"
                " ON CONFLICT (name) DO UPDATE SET name = excluded.name,"
                " items_json = excluded.items_json",
                (normalize_shortcut_name(name), json.dumps(payload, ensure_ascii=False)),
            )

    def get_shortcut(self, name: str) -> dict[str, Any] | None:
        """The stored meal payload for an exact (case-insensitive) name match."""
        row = self._conn.execute(
            "SELECT items_json FROM shortcuts WHERE name = ?",
            (normalize_shortcut_name(name),),
        ).fetchone()
        return json.loads(row["items_json"]) if row else None

    def list_shortcuts(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT name, items_json FROM shortcuts ORDER BY name"
        ).fetchall()
        return [(row["name"], json.loads(row["items_json"])) for row in rows]

    def delete_shortcut(self, name: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM shortcuts WHERE name = ?", (normalize_shortcut_name(name),)
            )
        return cursor.rowcount > 0

    # ---- pending (unconfirmed parses, failed requests kept for retry) ---

    def get_pending(self, chat_id: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload_json FROM pending WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else {}

    def set_pending(self, chat_id: int, payload: dict[str, Any]) -> None:
        with self._conn:
            if not payload:
                self._conn.execute("DELETE FROM pending WHERE chat_id = ?", (chat_id,))
                return
            self._conn.execute(
                "INSERT INTO pending (chat_id, payload_json) VALUES (?, ?)"
                " ON CONFLICT (chat_id) DO UPDATE SET payload_json = excluded.payload_json",
                (chat_id, json.dumps(payload, ensure_ascii=False)),
            )

    def clear_pending(self, chat_id: int) -> None:
        self.set_pending(chat_id, {})
