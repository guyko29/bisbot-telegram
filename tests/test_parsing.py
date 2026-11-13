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
        categories=(Category("carbs", "פחמימות", daily_target=6),),
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
    categories, meal_types, placeholder = parse_targets(
        {
            "categories": [
                {"key": "carbs", "name": "פחמימות", "daily_target": 6},
                {"key": "vegetables", "name": "ירקות", "daily_target": None},
            ]
        }
    )
    assert categories[0].daily_target == 6.0 and categories[0].unit == "מנות"
    assert categories[1].daily_target is None
    assert "pre_workout" in [m.key for m in meal_types]
    assert placeholder is False


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"categories": []},
        {"categories": [{"key": "carbs"}]},
        {"categories": [{"key": "a", "name": "א"}, {"key": "a", "name": "ב"}]},
        {"categories": [{"key": "a", "name": "א", "daily_target": "six"}]},
        {"categories": [{"key": "a", "name": "א", "daily_target": -1}]},
        {"categories": [{"key": "a", "name": "א"}], "meal_types": [{"key": "x"}]},
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
    assert menu.category("carbs").daily_target == 4.5
    assert menu.category("nope") is None


def test_load_menu_reports_missing_or_invalid_files(tmp_path):
    with pytest.raises(MenuError):
        load_menu(tmp_path / "missing.md", tmp_path / "targets.json")
    (tmp_path / "menu.md").write_text("x", encoding="utf-8")
    (tmp_path / "targets.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MenuError, match="not valid JSON"):
        load_menu(tmp_path / "menu.md", tmp_path / "targets.json")


def test_repository_menu_files_load():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    menu = load_menu(root / "menu.md", root / "menu_targets.json")
    assert menu.categories and menu.text
