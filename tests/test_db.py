from datetime import datetime, timezone

from bisbot.db import Database

from .conftest import NOW, make_meal


def add(db, meal, day="2026-11-13", **kwargs):
    return db.add_meal(meal, logged_at=NOW, day=day, **kwargs)


def test_add_meal_roundtrip(db):
    meal_id = add(
        db,
        make_meal(("לחם", "carbs", 2), ("ביצה", "protein", 1), notes="הערה"),
        raw_text="2 פרוסות לחם וביצה",
        photo_file_id="file-1",
    )
    stored = db.last_meal()

    assert stored.id == meal_id and stored.day == "2026-11-13"
    assert stored.raw_text == "2 פרוסות לחם וביצה" and stored.photo_file_id == "file-1"
    assert stored.meal.meal_type == "lunch" and stored.meal.notes == "הערה"
    assert [(i.name, i.category, i.portions) for i in stored.meal.items] == [
        ("לחם", "carbs", 2.0),
        ("ביצה", "protein", 1.0),
    ]
    # Stored in UTC, whatever the local timezone was.
    assert stored.logged_at == NOW and stored.logged_at.tzinfo == timezone.utc


def test_totals_are_summed_per_day_and_category(db):
    add(db, make_meal(("לחם", "carbs", 2), ("ביצה", "protein", 1)))
    add(db, make_meal(("אורז", "carbs", 1.5)))
    add(db, make_meal(("פיתה", "carbs", 2)), day="2026-11-12")

    assert db.totals_for_day("2026-11-13") == {"carbs": 3.5, "protein": 1.0}
    assert db.totals_for_day("2026-11-01") == {}
    assert db.totals_by_day("2026-11-12", "2026-11-13") == {
        "2026-11-12": {"carbs": 2.0},
        "2026-11-13": {"carbs": 3.5, "protein": 1.0},
    }


def test_deleted_meals_leave_totals_and_history(db):
    add(db, make_meal(("לחם", "carbs", 2)))
    second = add(db, make_meal(("אורז", "carbs", 1)))

    assert db.delete_meal(second) is True
    assert db.delete_meal(second) is False  # already gone
    assert db.totals_for_day("2026-11-13") == {"carbs": 2.0}
    assert [m.meal.items[0].name for m in db.meals_between("2026-11-13", "2026-11-13")] == ["לחם"]
    assert db.last_meal().meal.items[0].name == "לחם"


def test_last_meal_on_empty_database(db):
    assert db.last_meal() is None


def test_shortcuts_match_exactly_ignoring_case_and_spacing(db):
    db.save_shortcut("  Usual   Breakfast ", make_meal(("לחם", "carbs", 2), meal_type="breakfast"))
    db.save_shortcut("בוקר רגיל", make_meal(("ביצה", "protein", 2), meal_type="breakfast"))

    assert db.get_shortcut("usual breakfast")["items"][0]["name"] == "לחם"
    assert db.get_shortcut("בוקר  רגיל")["meal_type"] == "breakfast"
    assert db.get_shortcut("בוקר") is None  # no partial matches
    assert [name for name, _ in db.list_shortcuts()] == ["Usual Breakfast", "בוקר רגיל"]


def test_saving_a_shortcut_again_overwrites_it(db):
    db.save_shortcut("קבוע", make_meal(("לחם", "carbs", 2)))
    db.save_shortcut("קבוע", make_meal(("אורז", "carbs", 1)))

    assert len(db.list_shortcuts()) == 1
    assert db.get_shortcut("קבוע")["items"] == [
        {"name": "אורז", "category": "carbs", "portions": 1}
    ]


def test_delete_shortcut(db):
    db.save_shortcut("קבוע", make_meal(("לחם", "carbs", 2)))
    assert db.delete_shortcut("קבוע") is True
    assert db.delete_shortcut("קבוע") is False
    assert db.list_shortcuts() == []


def test_pending_payload_roundtrip(db):
    assert db.get_pending(1) == {}
    db.set_pending(1, {"meal": {"token": "abc", "raw_text": "לחם"}})
    db.set_pending(2, {"retry": {"token": "zzz"}})
    assert db.get_pending(1)["meal"]["raw_text"] == "לחם"

    db.set_pending(1, {"meal": {"token": "def"}})
    assert db.get_pending(1)["meal"]["token"] == "def"

    db.clear_pending(1)
    assert db.get_pending(1) == {} and db.get_pending(2) == {"retry": {"token": "zzz"}}


def test_data_survives_reopening_the_file(tmp_path):
    path = tmp_path / "nested" / "bisbot.db"
    first = Database(path)
    first.add_meal(
        make_meal(("לחם", "carbs", 2)),
        logged_at=datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc),
        day="2026-11-13",
    )
    first.close()

    second = Database(path)
    assert second.totals_for_day("2026-11-13") == {"carbs": 2.0}
    second.close()
