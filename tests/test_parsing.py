import pytest

from bisbot.menu import Category, MealType, Menu, MenuError, load_menu, parse_targets
from bisbot.parsing import MealValidationError, validate_meal


def payload(**overrides):
    base = {
        "meal_type": "breakfast",
        "items": [
            {"name": "2 פרוסות לחם", "category": "carbs", "portions": 2, "confidence": 0.95},
            {"name": "ביצה", "category": "protein", "portions": 1, "confidence": 0.9},
        ],
        "notes": "הנחתי לחם רגיל",
    }
    base.update(overrides)
    return base


def test_valid_meal_is_parsed(menu):
    meal = validate_meal(payload(), menu)
    assert meal.meal_type == "breakfast"
    assert [(i.name, i.category, i.portions) for i in meal.items] == [
        ("2 פרוסות לחם", "carbs", 2.0),
        ("ביצה", "protein", 1.0),
    ]
    assert meal.notes == "הנחתי לחם רגיל"
    assert meal.totals() == {"carbs": 2.0, "protein": 1.0}


def test_totals_sum_items_of_the_same_category(menu):
    meal = validate_meal(
        payload(
            items=[
                {"name": "לחם", "category": "carbs", "portions": 1.5, "confidence": 1},
                {"name": "אורז", "category": "carbs", "portions": 1, "confidence": 1},
            ]
        ),
        menu,
    )
    assert meal.totals() == {"carbs": 2.5}


def test_category_must_come_from_the_menu(menu):
    bad = payload(items=[{"name": "עוגה", "category": "dessert", "portions": 1, "confidence": 1}])
    with pytest.raises(MealValidationError, match="category 'dessert'"):
        validate_meal(bad, menu)


@pytest.mark.parametrize("items", [[], None, "לחם", [42]])
def test_items_must_be_a_non_empty_list_of_objects(menu, items):
    with pytest.raises(MealValidationError):
        validate_meal(payload(items=items), menu)


@pytest.mark.parametrize("portions", [0, -1, 21, "abc", None, True, float("nan"), float("inf")])
def test_bad_portions_are_rejected(menu, portions):
    bad = payload(items=[{"name": "לחם", "category": "carbs", "portions": portions}])
    with pytest.raises(MealValidationError):
        validate_meal(bad, menu)


def test_numeric_strings_are_accepted_and_rounded(menu):
    meal = validate_meal(
        payload(items=[{"name": "לחם", "category": "carbs", "portions": " 1.333 "}]), menu
    )
    assert meal.items[0].portions == 1.33


def test_confidence_defaults_to_one_and_is_clamped(menu):
    meal = validate_meal(
        payload(
            items=[
                {"name": "א", "category": "carbs", "portions": 1},
                {"name": "ב", "category": "carbs", "portions": 1, "confidence": 1.7},
                {"name": "ג", "category": "carbs", "portions": 1, "confidence": -0.2},
            ]
        ),
        menu,
    )
    assert [i.confidence for i in meal.items] == [1.0, 1.0, 0.0]


def test_empty_name_is_rejected(menu):
    bad = payload(items=[{"name": "  ", "category": "carbs", "portions": 1}])
    with pytest.raises(MealValidationError, match="name"):
        validate_meal(bad, menu)


def test_unknown_meal_type_falls_back_to_other(menu):
    assert validate_meal(payload(meal_type="brunch"), menu).meal_type == "other"


def test_unknown_meal_type_fails_when_menu_has_no_other():
    menu = Menu(
        text="x",
        categories=(Category("carbs", "פחמימות", daily_max=6),),
        meal_types=(MealType("breakfast", "בוקר"),),
    )
    with pytest.raises(MealValidationError, match="meal_type"):
        validate_meal(payload(meal_type="brunch", items=[
            {"name": "לחם", "category": "carbs", "portions": 1}
        ]), menu)


def test_non_dict_payload_and_non_string_notes(menu):
    with pytest.raises(MealValidationError):
        validate_meal(["not", "a", "dict"], menu)
    assert validate_meal(payload(notes=None), menu).notes == ""


# ---- menu targets file -------------------------------------------------


def test_parse_targets_reads_categories_and_defaults():
    parsed = parse_targets(
        {
            "categories": [
                {"key": "protein", "name": "חלבון", "daily_target": 3},
                {"key": "carbs", "name": "פחמימות", "daily_min": 3, "daily_max": 5},
                {"key": "vegetables", "name": "ירקות", "daily_min": 3},
                {"key": "snack", "name": "ביניים", "unit": "", "daily_max": 2},
                {"key": "pizza", "name": "פיצה", "weekly_limit": 1},
                {"key": "off_plan", "name": "מחוץ לתפריט"},
            ]
        }
    )
    protein, carbs, vegetables, snack, pizza, off_plan = parsed["categories"]

    assert (protein.daily_min, protein.daily_max, protein.is_exact) == (3.0, 3.0, True)
    assert (carbs.daily_min, carbs.daily_max, carbs.is_exact) == (3.0, 5.0, False)
    assert (vegetables.daily_min, vegetables.daily_max) == (3.0, None)
    assert (snack.daily_min, snack.daily_max, snack.unit) == (None, 2.0, "")
    assert pizza.is_weekly and pizza.weekly_limit == 1.0 and not pizza.has_daily_target
    assert not off_plan.has_daily_target and not off_plan.is_weekly
    assert protein.unit == "מנות"
    extra = {m.key: m.extra for m in parsed["meal_types"]}
    assert extra["pre_workout"] is True and extra["lunch"] is False
    assert parsed["placeholder"] is False and parsed["counting_rules"] == ""


def test_parse_targets_meal_types_and_counting_rules():
    parsed = parse_targets(
        {
            "categories": [{"key": "a", "name": "א"}],
            "meal_types": [
                {"key": "lunch", "name": "צהריים"},
                {"key": "pre", "name": "לפני אימון", "extra": True},
            ],
            "counting_rules": ["first rule ", "", "second rule"],
        }
    )
    menu = Menu(text="x", **parsed)

    assert menu.extra_meal_type_keys == ["pre"]
    assert menu.is_extra_meal("pre") and not menu.is_extra_meal("lunch")
    assert menu.counting_rules == "first rule\n\nsecond rule"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"categories": []},
        {"categories": [{"key": "carbs"}]},
        {"categories": [{"key": "a", "name": "א"}, {"key": "a", "name": "ב"}]},
        {"categories": [{"key": "a", "name": "א", "daily_target": "six"}]},
        {"categories": [{"key": "a", "name": "א", "daily_target": -1}]},
        {"categories": [{"key": "a", "name": "א", "daily_min": 5, "daily_max": 3}]},
        {"categories": [{"key": "a", "name": "א", "daily_target": 3, "daily_max": 4}]},
        {"categories": [{"key": "a", "name": "א", "daily_max": 2, "weekly_limit": 1}]},
        {"categories": [{"key": "a", "name": "א", "weekly_limit": True}]},
        {"categories": [{"key": "a", "name": "א"}], "meal_types": [{"key": "x"}]},
        {"categories": [{"key": "a", "name": "א"}], "counting_rules": 5},
    ],
)
def test_parse_targets_rejects_malformed_files(data):
    with pytest.raises(MenuError):
        parse_targets(data)


def test_load_menu_reads_both_files(tmp_path):
    (tmp_path / "menu.md").write_text("# התפריט שלי", encoding="utf-8")
    (tmp_path / "targets.json").write_text(
        '{"categories": [{"key": "carbs", "name": "פחמימות", "daily_target": 4.5}]}',
        encoding="utf-8",
    )
    menu = load_menu(tmp_path / "menu.md", tmp_path / "targets.json")
    assert menu.text == "# התפריט שלי"
    assert menu.category("carbs").daily_max == 4.5
    assert menu.category("nope") is None


def test_load_menu_reports_missing_or_invalid_files(tmp_path):
    with pytest.raises(MenuError):
        load_menu(tmp_path / "missing.md", tmp_path / "targets.json")
    (tmp_path / "menu.md").write_text("x", encoding="utf-8")
    (tmp_path / "targets.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MenuError, match="not valid JSON"):
        load_menu(tmp_path / "menu.md", tmp_path / "targets.json")


def test_repository_menu_is_the_real_plan(plan_menu):
    assert plan_menu.placeholder is False
    assert "200 גרם מנה חלבונית לאחר בישול" in plan_menu.text
    targets = {c.key: (c.daily_min, c.daily_max, c.weekly_limit) for c in plan_menu.categories}
    assert targets == {
        "carbs": (3.0, 5.0, None),
        "protein": (3.0, 3.0, None),
        "vegetables": (3.0, None, None),
        "snack": (None, 2.0, None),
        "pizza": (None, None, 1.0),
        "sushi": (None, None, 1.0),
        "golan_dessert": (None, None, 2.0),
        "alcohol": (None, None, 2.0),
        "off_plan": (None, None, None),
    }
    assert plan_menu.extra_meal_type_keys == ["pre_workout", "sport_extra"]
    assert "serving spoon" in plan_menu.counting_rules
