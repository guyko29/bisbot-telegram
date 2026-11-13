from datetime import date

from bisbot.summaries import (
    day_payload,
    day_status,
    format_balance,
    format_day,
    format_patterns,
    format_week,
    on_target_tolerance,
    week_days,
    week_payload,
    week_stats,
)
from bisbot.texts import fmt_num

FRIDAY = date(2026, 11, 13)


def by_key(statuses):
    return {s.category.key: s for s in statuses}


# ---- one day -----------------------------------------------------------


def test_day_status_remaining_and_over(menu):
    statuses = by_key(day_status({"carbs": 4, "protein": 5, "fat": 4.5, "vegetables": 3}, menu))

    assert (statuses["carbs"].eaten, statuses["carbs"].remaining, statuses["carbs"].over) == (4, 2, 0)
    assert (statuses["protein"].remaining, statuses["protein"].over) == (0, 0)
    assert (statuses["fat"].remaining, statuses["fat"].over) == (0, 1.5)
    # No target: tracked, but nothing "remains" and nothing is "over".
    assert statuses["vegetables"].remaining is None and statuses["vegetables"].over == 0


def test_day_status_covers_every_category_and_ignores_unknown_ones(menu):
    statuses = day_status({"carbs": 1, "removed_category": 9}, menu)
    assert [s.category.key for s in statuses] == ["carbs", "protein", "fat", "vegetables"]
    assert by_key(statuses)["protein"].eaten == 0


def test_format_day_shows_progress_per_category(menu):
    text = format_day(day_status({"carbs": 4, "protein": 5, "fat": 4, "vegetables": 1}, menu), FRIDAY)

    assert "יום שישי 13.11" in text
    assert "פחמימות: 4 מתוך 6 מנות · נשארו 2" in text
    assert "חלבון: 5 מתוך 5 מנות ✅" in text
    assert "שומן: 4 מתוך 3 מנות · חריגה של 1 ⚠️" in text
    assert "ירקות: 1 מנה (ללא יעד יומי)" in text
    assert "נשאר להיום: פחמימות 2" in text


def test_format_balance_lists_what_is_left_and_what_is_over(menu):
    assert format_balance(day_status({"carbs": 6, "protein": 3.5, "fat": 4}, menu)) == (
        "נשאר להיום: חלבון 1.5\n⚠️ מעבר ליעד: שומן 1"
    )
    assert format_balance(day_status({"carbs": 6, "protein": 5, "fat": 3}, menu)) == (
        "🎯 הגעת לכל היעדים להיום!"
    )


def test_day_payload_carries_the_exact_numbers(menu):
    payload = day_payload(day_status({"carbs": 4}, menu), FRIDAY)
    carbs = payload["categories"][0]
    assert payload["date"] == "2026-11-13"
    assert carbs == {
        "key": "carbs", "name": "פחמימות", "eaten": 4,
        "daily_target": 6, "remaining": 2, "over": 0,
    }


def test_fmt_num():
    assert [fmt_num(v) for v in (2.0, 1.5, 0.333, 0.25, 10)] == ["2", "1.5", "0.33", "0.25", "10"]


# ---- seven days --------------------------------------------------------


def test_week_days_ends_on_the_given_day():
    days = week_days(FRIDAY)
    assert len(days) == 7 and days[0] == date(2026, 11, 7) and days[-1] == FRIDAY


def test_tolerance_is_half_a_portion_or_ten_percent():
    assert on_target_tolerance(3) == 0.5
    assert on_target_tolerance(10) == 1.0


def test_week_stats_adherence_and_day_counts(menu):
    days = week_days(FRIDAY)
    totals = {
        days[0].isoformat(): {"carbs": 6, "protein": 5, "fat": 3},    # spot on
        days[1].isoformat(): {"carbs": 3, "protein": 5, "fat": 6},    # carbs -50%, fat +100%
        days[2].isoformat(): {"carbs": 9, "protein": 4.5, "fat": 3},  # carbs +50%, protein within tolerance
        # days[3] .. days[6]: nothing logged
    }
    stats = week_stats(totals, menu, days)
    cats = {c.category.key: c for c in stats.categories}

    assert stats.logged_days == days[:3]
    assert stats.unlogged_days == days[3:]

    carbs = cats["carbs"]
    assert carbs.total == 18 and carbs.avg_per_logged_day == 6
    # Scores 1.0, 0.5, 0.5 -> 66.7%. Unlogged days do not count as zeros.
    assert carbs.adherence_pct == 66.7
    assert (carbs.days_on_target, carbs.days_under, carbs.days_over) == (1, 1, 1)

    protein = cats["protein"]
    assert protein.adherence_pct == 96.7  # 1, 1, 0.9
    assert (protein.days_on_target, protein.days_under, protein.days_over) == (3, 0, 0)

    fat = cats["fat"]
    assert fat.adherence_pct == 66.7  # 1, 0, 1
    assert fat.days_over == 1

    # No target -> averages only.
    assert cats["vegetables"].adherence_pct is None
    assert stats.overall_adherence_pct == 76.7


def test_week_stats_adherence_never_goes_below_zero(menu):
    days = week_days(FRIDAY)
    stats = week_stats({days[0].isoformat(): {"fat": 30}}, menu, days)
    fat = next(c for c in stats.categories if c.category.key == "fat")
    assert fat.adherence_pct == 0.0


def test_week_stats_with_no_logs(menu):
    stats = week_stats({}, menu, week_days(FRIDAY))
    assert stats.logged_days == []
    assert stats.overall_adherence_pct is None
    assert all(c.adherence_pct is None and c.total == 0 for c in stats.categories)


def test_patterns_flag_consistent_misses_and_missing_days(menu):
    days = week_days(FRIDAY)
    totals = {
        d.isoformat(): {"carbs": 6, "protein": 2, "fat": 5} for d in days[:5]
    }
    stats = week_stats(totals, menu, days)
    joined = "\n".join(stats.patterns)

    assert "ימים בלי תיעוד" in joined
    assert "חלבון: מתחת ליעד ב-5 מתוך 5 ימים (ממוצע 2 מתוך 5)" in joined
    assert "שומן: מעל היעד ב-5 מתוך 5 ימים" in joined
    assert "הכי קרוב ליעד: פחמימות (100%)" in joined
    assert format_patterns(stats).startswith("מה בלט השבוע:")


def test_no_category_patterns_from_too_few_days(menu):
    days = week_days(FRIDAY)
    stats = week_stats({days[0].isoformat(): {"carbs": 1}}, menu, days)
    assert len(stats.patterns) == 1 and stats.patterns[0].startswith("ימים בלי תיעוד")


def test_format_week_and_payload(menu):
    days = week_days(FRIDAY)
    totals = {d.isoformat(): {"carbs": 6, "protein": 4, "fat": 3, "vegetables": 2} for d in days}
    stats = week_stats(totals, menu, days)
    text = format_week(stats)

    assert "סיכום שבועי · 7.11 עד 13.11" in text
    assert "ימים עם תיעוד: 7 מתוך 7" in text
    assert "פחמימות: ממוצע 6 מתוך 6 מנות ליום · עמידה ביעד 100%" in text
    assert "חלבון: ממוצע 4 מתוך 5 מנות ליום · עמידה ביעד 80%" in text
    assert "ביעד 0 ימים · מתחת 7 · מעל 0" in text
    assert "ירקות: ממוצע 2 מנות ליום" in text

    payload = week_payload(stats)
    assert payload["first_day"] == "2026-11-07" and payload["days_without_any_log"] == []
    protein = next(c for c in payload["categories"] if c["key"] == "protein")
    assert protein["adherence_pct"] == 80.0 and protein["days_under_target"] == 7
    assert payload["portions_per_day"]["2026-11-13"]["carbs"] == 6
