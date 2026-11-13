"""Every Hebrew string the bot sends, plus small formatting helpers.

Messages are sent as plain text (no Markdown/HTML parse mode), so nothing here
or in Claude's answers needs escaping.
"""

from __future__ import annotations

from datetime import date

from .menu import Category, Menu
from .models import ParsedMeal

LOW_CONFIDENCE = 0.6

BTN_CONFIRM = "✅ אישור"
BTN_FIX = "✏️ תיקון"
BTN_CANCEL = "🗑 ביטול"
BTN_RETRY = "🔄 נסה שוב"

HELP = (
    "היי! אני BisBot 🥗 עוזר לך לעקוב אחרי התפריט מהדיאטנית.\n"
    "\n"
    "איך מתעדים ארוחה?\n"
    "פשוט כותבים מה אכלת (למשל: \"2 פרוסות לחם, ביצה וסלט\") או שולחים תמונה, "
    "ואני אפרק את זה למנות. שום דבר לא נשמר לפני שלוחצים ✅.\n"
    "\n"
    "פקודות:\n"
    "/today - מה אכלת היום ומה נשאר\n"
    "/week - סיכום 7 הימים האחרונים\n"
    "/undo - מחיקת הארוחה האחרונה שנרשמה\n"
    "/save <שם> - שמירת הארוחה האחרונה כקיצור\n"
    "/shortcuts - רשימת הקיצורים\n"
    "/delshortcut <שם> - מחיקת קיצור\n"
    "\n"
    "אפשר גם לשאול אותי כל דבר: רעיונות לארוחת ערב לפי מה שנשאר היום, "
    "תחליפים, או מה לאכול לפני אימון בשעה מסוימת."
)

PLACEHOLDER_WARNING = (
    "⚠️ שים לב: התפריט שמוגדר כרגע הוא תפריט לדוגמה ולא התפריט האמיתי שלך. "
    "יש להחליף את menu.md ואת menu_targets.json."
)

ASK_FIX = "מה לתקן? כתוב לי בחופשיות (למשל: \"זה היה 3 פרוסות, ובלי החמאה\")."
SAVED = "✅ נשמר!"
CANCELLED = "🗑 בוטל, לא נשמר כלום."
STALE_BUTTON = "הכפתור הזה כבר לא בתוקף."
PENDING_REMINDER = "הארוחה הזו עדיין מחכה לאישור:"
SAVE_HINT = "טיפ: /save <שם> ישמור את הארוחה הזו כקיצור."
RETRYING = "מנסה שוב..."

UNSUPPORTED_MESSAGE = "אני מבין כרגע רק טקסט או תמונה 🙂"
IMAGE_TOO_LARGE = "התמונה גדולה מדי. נסה לשלוח אותה כתמונה רגילה (לא כקובץ)."
UNKNOWN_COMMAND = "לא מכיר את הפקודה הזו. /help מציג את כל הפקודות."
UNEXPECTED_ERROR = "😕 משהו השתבש אצלי. נסה שוב בעוד רגע."

UNDO_NOTHING = "אין ארוחה למחוק."
UNDO_DONE = "↩️ מחקתי את הארוחה האחרונה:"

SAVE_USAGE = "כדי לשמור קיצור: /save <שם>, למשל: /save בוקר רגיל"
SAVE_NO_MEAL = "אין עדיין ארוחה שמורה שאפשר להפוך לקיצור. קודם רושמים ארוחה ומאשרים אותה."
SAVE_BAD_NAME = "שם הקיצור צריך להיות עד 50 תווים ולא להתחיל ב־/."
SHORTCUTS_EMPTY = "אין עדיין קיצורים. אחרי שמאשרים ארוחה אפשר לשמור אותה עם /save <שם>."
SHORTCUTS_TITLE = "⚡ הקיצורים שלך (שולחים את השם בדיוק כדי לרשום):"
DELSHORTCUT_USAGE = "כדי למחוק קיצור: /delshortcut <שם>"
SHORTCUT_INVALID = (
    "הקיצור \"{name}\" לא תואם יותר לתפריט הנוכחי, ולכן לא נרשם. "
    "כדאי למחוק אותו עם /delshortcut ולשמור מחדש."
)

NOTHING_LOGGED_TODAY = "עדיין לא נרשמו ארוחות היום."
NOTHING_LOGGED_WEEK = "לא נרשמו ארוחות בשבעת הימים האחרונים, אז אין עדיין מה לסכם."
CLAUDE_NOTE_UNAVAILABLE = "(לא הצלחתי לנסח תובנות כרגע, אבל המספרים למעלה מדויקים.)"

CLAUDE_ERRORS = {
    "rate_limit": "⏳ יש כרגע עומס על Claude.",
    "connection": "📡 לא הצלחתי להתחבר ל-Claude.",
    "auth": "🔑 יש בעיה במפתח ה-API של Claude (צריך לבדוק את ANTHROPIC_API_KEY).",
    "refusal": "🤔 Claude לא הסכים לענות על ההודעה הזו.",
    "invalid_output": "🤔 לא הצלחתי להבין את התשובה של Claude.",
    "telegram": "📡 לא הצלחתי להוריד את התמונה מטלגרם.",
}
CLAUDE_ERROR_DEFAULT = "😕 משהו השתבש בפנייה ל-Claude."
CLAUDE_ERROR_KEPT = "שמרתי את מה ששלחת, אפשר ללחוץ על \"נסה שוב\"."

WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # Monday = 0


def fmt_num(value: float) -> str:
    """2.0 -> '2', 1.5 -> '1.5', 0.333 -> '0.33'."""
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def fmt_qty(value: float, category: Category) -> str:
    """'1 מנה' / '2.5 מנות'."""
    unit = category.unit_singular if round(value, 2) == 1 else category.unit
    return f"{fmt_num(value)} {unit}"


def fmt_day(day: date) -> str:
    """'יום חמישי 13.11'."""
    return f"יום {WEEKDAYS[day.weekday()]} {day.day}.{day.month}"


def claude_error(kind: str) -> str:
    return f"{CLAUDE_ERRORS.get(kind, CLAUDE_ERROR_DEFAULT)}\n{CLAUDE_ERROR_KEPT}"


def shortcut_saved(name: str) -> str:
    return f"⚡ נשמר הקיצור \"{name}\". מעכשיו מספיק לשלוח את השם הזה כדי לרשום את הארוחה."


def shortcut_deleted(name: str) -> str:
    return f"🗑 הקיצור \"{name}\" נמחק."


def shortcut_not_found(name: str) -> str:
    return f"לא מצאתי קיצור בשם \"{name}\". /shortcuts מציג את הרשימה."


def shortcut_logged(name: str) -> str:
    return f"⚡ נרשם מהקיצור \"{name}\":"


def format_totals(totals: dict[str, float], menu: Menu) -> str:
    """'פחמימות 2 · חלבון 1' in menu order."""
    parts = [
        f"{category.name} {fmt_num(totals[category.key])}"
        for category in menu.categories
        if totals.get(category.key)
    ]
    return " · ".join(parts)


def format_meal(meal: ParsedMeal, menu: Menu, *, show_confidence: bool = True) -> str:
    """Multi-line Hebrew description of a meal and its portions per category."""
    lines = [f"🍽 {menu.meal_type_name(meal.meal_type)}"]
    unsure = False
    for item in meal.items:
        category = menu.category(item.category)
        if category is None:
            line = f"• {item.name} - {item.category}: {fmt_num(item.portions)}"
        else:
            line = f"• {item.name} - {category.name}: {fmt_qty(item.portions, category)}"
        if show_confidence and item.confidence < LOW_CONFIDENCE:
            line += " ❓"
            unsure = True
        lines.append(line)
    if len(meal.items) > 1:
        lines.append(f"סה״כ: {format_totals(meal.totals(), menu)}")
    if meal.notes:
        lines.append(f"📝 {meal.notes}")
    if unsure:
        lines.append("❓ = לא בטוח בהערכה, כדאי לבדוק")
    return "\n".join(lines)


def format_shortcut_line(name: str, payload: dict, menu: Menu) -> str:
    totals: dict[str, float] = {}
    for item in payload.get("items", []):
        try:
            totals[item["category"]] = totals.get(item["category"], 0.0) + float(item["portions"])
        except (KeyError, TypeError, ValueError):
            continue
    summary = format_totals(totals, menu)
    return f"• {name} - {summary}" if summary else f"• {name}"
