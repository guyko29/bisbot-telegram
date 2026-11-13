"""Bot flows against the real plan in the repository (menu.md + menu_targets.json)."""

from datetime import timedelta

import pytest

from bisbot import texts

from .conftest import NOW, log_result, make_meal

TODAY = NOW.date().isoformat()

BREAKFAST = make_meal(
    ("2 פרוסות לחם", "carbs", 2),
    ("2 ביצים", "protein", 1),
    ("מלפפון ועגבנייה", "vegetables", 2),
    meal_type="breakfast",
)
PRE_WORKOUT = make_meal(
    ("טורטייה", "carbs", 1), ("2 כפות גבינה 5%", "protein", 1), meal_type="pre_workout"
)
PIZZA_DINNER = make_meal(
    ("2 משולשי פיצה", "pizza", 1),
    ("פיצה - בצק", "carbs", 2),
    ("פיצה - גבינה", "protein", 1),
    ("סלט ירוק", "vegetables", 1),
    meal_type="dinner",
)


@pytest.fixture
def menu(plan_menu):
    """Every fixture in this module (bot, reports...) runs on the real plan."""
    return plan_menu


def days_ago(count: int):
    moment = NOW - timedelta(days=count)
    return moment, moment.date().isoformat()


async def test_today_shows_ranges_and_what_is_left(bot, db):
    db.add_meal(BREAKFAST, logged_at=NOW, day=TODAY)

    await bot.text("/today")

    reply = bot.request.texts()[-1]
    assert "פחמימות: 2 מתוך 3 עד 5 מנות · נשארו 1 עד 3" in reply
    assert "חלבון: 1 מתוך 3 מנות · נשארו 2" in reply
    assert "ירקות: 2 מתוך 3 מנות לפחות · נשארו 1" in reply
    assert "ביניים: 0 מתוך 2 לכל היותר ✅ אפשר עוד 2" in reply
    assert "נשאר להיום: פחמימות 1 עד 3 · חלבון 2 · ירקות לפחות 1 · ביניים עד 2" in reply
    assert texts.PLACEHOLDER_WARNING not in reply
    assert texts.WEEK_SO_FAR not in reply  # nothing weekly-limited was eaten


async def test_pre_workout_snack_stays_out_of_the_daily_totals(bot, claude, db):
    db.add_meal(BREAKFAST, logged_at=NOW, day=TODAY)
    claude.handle_message.return_value = log_result(PRE_WORKOUT)

    await bot.text("טורטייה עם גבינה לפני האימון")
    assert texts.EXTRA_MEAL_NOTE in bot.request.texts()[-1]
    await bot.click_button("✅")

    # Stored like any meal...
    assert db.last_meal().meal.meal_type == "pre_workout"
    assert db.totals_for_day(TODAY) == {"carbs": 3.0, "protein": 2.0, "vegetables": 2.0}
    # ...but the day's balance still reflects the breakfast only.
    saved = bot.request.sent("editMessageText")[-1]["text"]
    assert "נשאר להיום: פחמימות 1 עד 3 · חלבון 2 · ירקות לפחות 1 · ביניים עד 2" in saved

    await bot.text("/today")
    today = bot.request.texts()[-1]
    assert "פחמימות: 2 מתוך 3 עד 5 מנות" in today and "חלבון: 1 מתוך 3 מנות" in today
    assert f"{texts.EXTRAS_TODAY_TITLE}\n• לפני אימון: טורטייה, 2 כפות גבינה 5%" in today


async def test_claude_context_marks_extras_and_weekly_counts(bot, claude, db):
    db.add_meal(BREAKFAST, logged_at=NOW, day=TODAY)
    db.add_meal(PRE_WORKOUT, logged_at=NOW, day=TODAY)
    moment, day = days_ago(3)  # Tuesday of the same Sunday-Saturday week
    db.add_meal(PIZZA_DINNER, logged_at=moment, day=day)

    context = bot.handlers.reports.claude_context(NOW)

    assert "- carbs (פחמימות): eaten 2, target 3 to 5, still needs 1 (up to 3 fits)" in context
    assert "- vegetables (ירקות): eaten 2, target at least 3, still needs 1" in context
    assert "- snack (ביניים): eaten 0, target at most 2, target met, up to 2 more fits" in context
    assert "- pizza (פיצה): 1 of at most 1" in context
    assert "- sushi (סושי): 0 of at most 1" in context
    assert "pre_workout [extra, not in totals]: טורטייה (carbs 1)" in context


async def test_second_pizza_in_the_same_week_is_flagged(bot, claude, db):
    moment, day = days_ago(3)
    db.add_meal(PIZZA_DINNER, logged_at=moment, day=day)
    claude.handle_message.return_value = log_result(PIZZA_DINNER)

    await bot.text("2 משולשי פיצה וסלט")
    await bot.click_button("✅")

    saved = bot.request.sent("editMessageText")[-1]["text"]
    assert f"{texts.WEEK_SO_FAR} פיצה 2 מתוך 1 ⚠️" in saved
    assert texts.WEEKLY_LIMIT_EXCEEDED in saved

    await bot.text("/today")
    assert f"{texts.WEEK_SO_FAR} פיצה 2 מתוך 1 ⚠️" in bot.request.texts()[-1]


async def test_first_pizza_of_the_week_is_within_the_limit(bot, claude, db):
    moment, day = days_ago(6)  # last Saturday: the previous calendar week
    db.add_meal(PIZZA_DINNER, logged_at=moment, day=day)
    claude.handle_message.return_value = log_result(PIZZA_DINNER)

    await bot.text("פיצה")
    await bot.click_button("✅")

    saved = bot.request.sent("editMessageText")[-1]["text"]
    assert f"{texts.WEEK_SO_FAR} פיצה 1 מתוך 1" in saved
    assert texts.WEEKLY_LIMIT_EXCEEDED not in saved


async def test_week_report_on_the_real_plan(bot, claude, db):
    for offset in range(1, 6):
        moment, day = days_ago(offset)
        db.add_meal(BREAKFAST, logged_at=moment, day=day)
        db.add_meal(PRE_WORKOUT, logged_at=moment, day=day)
    moment, day = days_ago(2)
    db.add_meal(PIZZA_DINNER, logged_at=moment, day=day)

    await bot.text("/week")

    reply = bot.request.texts()[-1]
    assert "ימים עם תיעוד: 5 מתוך 7" in reply
    # 2 carbs a day from breakfast, plus 2 on the pizza day; the tortillas don't count.
    assert "פחמימות: ממוצע 2.4 ליום (יעד 3 עד 5)" in reply
    assert "מגבלות שבועיות: פיצה 1 מתוך 1 · סושי 0 מתוך 1 · מעדן הגולן 0 מתוך 2 · אלכוהול 0 מתוך 2" in reply
    payload = claude.phrase_weekly.await_args.args[0]
    assert payload["portions_per_day"][days_ago(1)[1]] == {
        "carbs": 2.0, "protein": 1.0, "vegetables": 2.0,
    }


async def test_daily_report_payload_separates_extras(bot, claude, db):
    db.add_meal(BREAKFAST, logged_at=NOW, day=TODAY)
    db.add_meal(PRE_WORKOUT, logged_at=NOW, day=TODAY)

    report = await bot.handlers.reports.daily_report(NOW)

    assert texts.EXTRAS_TODAY_TITLE in report
    payload = claude.phrase_daily.await_args.args[0]
    counted = {m["meal_type"]: m["counted_in_daily_totals"] for m in payload["meals"]}
    assert counted == {"breakfast": True, "pre_workout": False}
    carbs = payload["categories"][0]
    assert (carbs["eaten"], carbs["daily_min"], carbs["daily_max"]) == (2, 3, 5)
    assert payload["weekly_limited_items_since_sunday"][0]["key"] == "pizza"


def test_db_totals_can_exclude_meal_types(db):
    db.add_meal(BREAKFAST, logged_at=NOW, day=TODAY)
    db.add_meal(PRE_WORKOUT, logged_at=NOW, day=TODAY)

    assert db.totals_for_day(TODAY)["carbs"] == 3.0
    excluded = db.totals_for_day(TODAY, ["pre_workout", "sport_extra"])
    assert excluded == {"carbs": 2.0, "protein": 1.0, "vegetables": 2.0}
    # A day that only has extra meals has no counted totals at all.
    moment, day = days_ago(1)
    db.add_meal(PRE_WORKOUT, logged_at=moment, day=day)
    assert db.totals_by_day(day, day, ["pre_workout"]) == {}
