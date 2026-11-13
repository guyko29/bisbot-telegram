"""End-to-end flows through the real Application, with Telegram and Claude faked."""

from datetime import datetime, timedelta

from bisbot import texts
from bisbot.claude_client import ClaudeError, RouterResult
from bisbot.jobs import DAILY_JOB, WEEKLY_JOB

from .conftest import NOW, OTHER_CHAT, TZ, log_result, make_meal

TODAY = NOW.date().isoformat()
BREAD_AND_EGG = make_meal(("2 פרוסות לחם", "carbs", 2), ("ביצה", "protein", 1), meal_type="breakfast")


def answer(text: str, intent: str = "question") -> RouterResult:
    return RouterResult(intent=intent, answer=text)


# ---- access control ----------------------------------------------------


async def test_other_chats_are_ignored_completely(bot, claude, db):
    await bot.text("2 פרוסות לחם", chat_id=OTHER_CHAT)
    await bot.text("/today", chat_id=OTHER_CHAT)
    await bot.photo(chat_id=OTHER_CHAT)
    await bot.click("c:whatever", chat_id=OTHER_CHAT)

    claude.handle_message.assert_not_awaited()
    assert bot.request.calls == [("getMe", {})]
    assert db.get_pending(OTHER_CHAT) == {}


# ---- logging a meal ----------------------------------------------------


async def test_text_is_parsed_but_saved_only_after_confirm(bot, claude, db):
    claude.handle_message.return_value = log_result(BREAD_AND_EGG)

    await bot.text("2 פרוסות לחם וביצה")

    summary = bot.request.texts()[-1]
    assert "ארוחת בוקר" in summary
    assert "• 2 פרוסות לחם - פחמימות: 2 מנות" in summary
    assert "• ביצה - חלבון: 1 מנה" in summary
    assert [b["text"] for b in bot.request.last_buttons()] == [
        texts.BTN_CONFIRM, texts.BTN_FIX, texts.BTN_CANCEL,
    ]
    assert db.last_meal() is None, "nothing may be stored before Confirm"
    assert claude.handle_message.await_args.kwargs["text"] == "2 פרוסות לחם וביצה"

    await bot.click_button("✅")

    stored = db.last_meal()
    assert stored.raw_text == "2 פרוסות לחם וביצה" and stored.day == TODAY
    assert db.totals_for_day(TODAY) == {"carbs": 2.0, "protein": 1.0}
    assert db.get_pending(bot.handlers.settings.allowed_chat_id) == {}
    edited = bot.request.sent("editMessageText")[-1]["text"]
    assert texts.SAVED in edited and "נשאר להיום: פחמימות 4 · חלבון 4 · שומן 3" in edited


async def test_low_confidence_items_are_flagged(bot, claude):
    meal = make_meal(("אורז", "carbs", 1.5))
    meal.items[0].confidence = 0.4
    claude.handle_message.return_value = log_result(meal)

    await bot.text("קצת אורז")

    assert "• אורז - פחמימות: 1.5 מנות ❓" in bot.request.texts()[-1]


async def test_confirm_twice_does_not_save_twice(bot, claude, db):
    claude.handle_message.return_value = log_result(BREAD_AND_EGG)
    await bot.text("לחם וביצה")
    callback = bot.request.last_buttons()[0]["callback_data"]

    await bot.click(callback)
    await bot.click(callback)

    assert len(db.meals_between(TODAY, TODAY)) == 1
    stale = bot.request.sent("answerCallbackQuery")[-1]
    assert stale["text"] == texts.STALE_BUTTON


async def test_cancel_discards_the_parse(bot, claude, db):
    claude.handle_message.return_value = log_result(BREAD_AND_EGG)
    await bot.text("לחם וביצה")

    await bot.click_button("🗑")

    assert db.last_meal() is None
    assert db.get_pending(bot.handlers.settings.allowed_chat_id) == {}
    assert texts.CANCELLED in bot.request.sent("editMessageText")[-1]["text"]


async def test_fix_reparses_with_the_previous_result_as_context(bot, claude, db):
    one_slice = make_meal(("פרוסת לחם", "carbs", 1), meal_type="breakfast")
    two_slices = make_meal(("2 פרוסות לחם", "carbs", 2), meal_type="breakfast")
    claude.handle_message.side_effect = [log_result(one_slice), log_result(two_slices)]

    await bot.text("לחם")
    await bot.click_button("✏️")
    assert bot.request.texts()[-1] == texts.ASK_FIX

    await bot.text("זה היה 2 פרוסות")

    fix_call = claude.handle_message.await_args.kwargs
    assert fix_call["text"] == "לחם"
    assert fix_call["correction"] == "זה היה 2 פרוסות"
    assert fix_call["previous"].to_dict() == one_slice.to_dict()
    assert "פחמימות: 2 מנות" in bot.request.texts()[-1]
    assert db.last_meal() is None, "a fixed meal still needs Confirm"

    await bot.click_button("✅")

    stored = db.last_meal()
    assert stored.meal.totals() == {"carbs": 2.0}
    assert stored.raw_text == "לחם\n[תיקון] זה היה 2 פרוסות"


async def test_a_new_meal_replaces_an_unconfirmed_one(bot, claude, db):
    claude.handle_message.side_effect = [
        log_result(BREAD_AND_EGG),
        log_result(make_meal(("אורז", "carbs", 1))),
    ]
    await bot.text("לחם וביצה")
    old_confirm = bot.request.last_buttons()[0]["callback_data"]
    await bot.text("אורז")

    await bot.click(old_confirm)
    assert db.last_meal() is None, "the old button must not save anything"

    await bot.click_button("✅")
    assert db.last_meal().meal.totals() == {"carbs": 1.0}


async def test_photo_with_caption_is_sent_to_claude(bot, claude, db):
    claude.handle_message.return_value = log_result(make_meal(("שניצל", "protein", 3)))

    await bot.photo(caption="ארוחת צהריים")

    kwargs = claude.handle_message.await_args.kwargs
    assert kwargs["text"] == "ארוחת צהריים"
    assert kwargs["image"] == b"fake-jpeg-bytes" and kwargs["image_media_type"] == "image/jpeg"
    assert bot.request.sent("getFile")[-1]["file_id"] == "large"

    await bot.click_button("✅")
    assert db.last_meal().photo_file_id == "large"


# ---- questions ---------------------------------------------------------


async def test_question_gets_an_answer_and_today_context(bot, claude, db):
    db.add_meal(BREAD_AND_EGG, logged_at=NOW - timedelta(hours=6), day=TODAY)
    claude.handle_message.return_value = answer("אפשר חזה עוף עם אורז וסלט.")

    await bot.text("מה כדאי לאכול בערב?")

    assert bot.request.texts()[-1] == "אפשר חזה עוף עם אורז וסלט."
    assert db.get_pending(bot.handlers.settings.allowed_chat_id) == {}
    context = claude.handle_message.await_args.kwargs["context"]
    assert "Current local time: 2026-11-13 14:30 (Friday)" in context
    assert "- carbs (פחמימות): eaten 2, target 6, still needs 4" in context
    assert "- vegetables (ירקות): eaten 0, no daily target" in context
    assert "- 08:30 breakfast: 2 פרוסות לחם (carbs 2), ביצה (protein 1)" in context


async def test_follow_up_questions_see_the_recent_conversation(bot, claude):
    claude.handle_message.side_effect = [answer("טוסט עם קוטג'."), answer("כן, גם פיתה.")]

    await bot.text("מה לאכול לפני אימון ב-18:00?")
    await bot.text("ואפשר להחליף את הלחם?")

    context = claude.handle_message.await_args.kwargs["context"]
    assert "user: מה לאכול לפני אימון ב-18:00?" in context
    assert "bot: טוסט עם קוטג'." in context
    assert "ואפשר להחליף את הלחם?" not in context  # the current message is sent separately


async def test_question_keeps_a_meal_that_waits_for_confirmation(bot, claude, db):
    claude.handle_message.side_effect = [log_result(BREAD_AND_EGG), answer("6 מנות ביום.")]
    await bot.text("לחם וביצה")
    await bot.text("כמה פחמימות מותר לי?")

    await bot.click_button("✅")
    assert db.last_meal() is not None


# ---- failures ----------------------------------------------------------


async def test_claude_failure_keeps_the_raw_text_for_retry(bot, claude, db):
    chat_id = bot.handlers.settings.allowed_chat_id
    claude.handle_message.side_effect = [
        ClaudeError("connection", "timeout"),
        log_result(BREAD_AND_EGG),
    ]

    await bot.text("2 פרוסות לחם וביצה")

    message = bot.request.texts()[-1]
    assert texts.CLAUDE_ERRORS["connection"] in message and texts.CLAUDE_ERROR_KEPT in message
    assert db.get_pending(chat_id)["retry"]["raw_text"] == "2 פרוסות לחם וביצה"
    assert [b["text"] for b in bot.request.last_buttons()] == [texts.BTN_RETRY]

    await bot.click_button("🔄")

    assert claude.handle_message.await_args.kwargs["text"] == "2 פרוסות לחם וביצה"
    assert "retry" not in db.get_pending(chat_id)
    await bot.click_button("✅")
    assert db.totals_for_day(TODAY) == {"carbs": 2.0, "protein": 1.0}


async def test_failure_during_fix_keeps_the_meal_and_the_correction(bot, claude, db):
    chat_id = bot.handlers.settings.allowed_chat_id
    fixed = make_meal(("3 פרוסות לחם", "carbs", 3))
    claude.handle_message.side_effect = [
        log_result(make_meal(("לחם", "carbs", 1))),
        ClaudeError("rate_limit"),
        log_result(fixed),
    ]
    await bot.text("לחם")
    await bot.click_button("✏️")
    await bot.text("3 פרוסות")

    pending = db.get_pending(chat_id)
    assert pending["retry"]["correction"] == "3 פרוסות"
    assert pending["meal"]["parsed"]["items"][0]["portions"] == 1

    await bot.click_button("🔄")
    assert claude.handle_message.await_args.kwargs["correction"] == "3 פרוסות"
    await bot.click_button("✅")
    assert db.last_meal().meal.totals() == {"carbs": 3.0}


async def test_unexpected_errors_are_reported_in_hebrew(bot, claude):
    claude.handle_message.side_effect = RuntimeError("bug")

    await bot.text("לחם")

    assert bot.request.texts()[-1] == texts.UNEXPECTED_ERROR


# ---- shortcuts and undo ------------------------------------------------


async def test_shortcut_logs_directly_without_claude(bot, claude, db):
    claude.handle_message.return_value = log_result(BREAD_AND_EGG)
    await bot.text("לחם וביצה")
    await bot.click_button("✅")
    await bot.text("/save בוקר רגיל")
    assert texts.shortcut_saved("בוקר רגיל") in bot.request.texts()[-1]
    claude.handle_message.reset_mock()

    await bot.text("בוקר  רגיל")

    claude.handle_message.assert_not_awaited()
    assert len(db.meals_between(TODAY, TODAY)) == 2
    assert db.totals_for_day(TODAY) == {"carbs": 4.0, "protein": 2.0}
    reply = bot.request.texts()[-1]
    assert texts.shortcut_logged("בוקר רגיל") in reply and "נשאר להיום" in reply

    # Only an exact match is a shortcut; anything else still goes to Claude.
    claude.handle_message.return_value = answer("?", "other")
    await bot.text("בוקר רגיל ועוד תפוח")
    claude.handle_message.assert_awaited_once()


async def test_shortcut_commands(bot, claude, db):
    await bot.text("/save משהו")
    assert bot.request.texts()[-1] == texts.SAVE_NO_MEAL
    await bot.text("/shortcuts")
    assert bot.request.texts()[-1] == texts.SHORTCUTS_EMPTY

    db.add_meal(BREAD_AND_EGG, logged_at=NOW, day=TODAY)
    await bot.text("/save")
    assert bot.request.texts()[-1] == texts.SAVE_USAGE
    await bot.text("/save בוקר רגיל")
    await bot.text("/shortcuts")
    assert "• בוקר רגיל - פחמימות 2 · חלבון 1" in bot.request.texts()[-1]

    await bot.text("/delshortcut בוקר רגיל")
    assert bot.request.texts()[-1] == texts.shortcut_deleted("בוקר רגיל")
    await bot.text("/delshortcut בוקר רגיל")
    assert bot.request.texts()[-1] == texts.shortcut_not_found("בוקר רגיל")
    assert db.list_shortcuts() == []


async def test_shortcut_that_no_longer_fits_the_menu_is_not_logged(bot, db):
    db.save_shortcut("ישן", make_meal(("יוגורט", "dairy", 1)))

    await bot.text("ישן")

    assert db.last_meal() is None
    assert bot.request.texts()[-1] == texts.SHORTCUT_INVALID.format(name="ישן")


async def test_undo_removes_the_last_meal(bot, db):
    db.add_meal(BREAD_AND_EGG, logged_at=NOW, day=TODAY)
    db.add_meal(make_meal(("אורז", "carbs", 2)), logged_at=NOW, day=TODAY)

    await bot.text("/undo")

    assert db.totals_for_day(TODAY) == {"carbs": 2.0, "protein": 1.0}
    reply = bot.request.texts()[-1]
    assert texts.UNDO_DONE in reply and "אורז" in reply

    await bot.text("/undo")
    await bot.text("/undo")
    assert bot.request.texts()[-1] == texts.UNDO_NOTHING


# ---- summaries ---------------------------------------------------------


async def test_today_shows_totals_targets_and_what_is_left(bot, claude, db):
    db.add_meal(BREAD_AND_EGG, logged_at=NOW, day=TODAY)
    db.add_meal(make_meal(("אבוקדו", "fat", 4)), logged_at=NOW, day=TODAY)
    db.add_meal(make_meal(("פיתה", "carbs", 2)), logged_at=NOW, day="2026-11-12")

    await bot.text("/today")

    reply = bot.request.texts()[-1]
    assert "פחמימות: 2 מתוך 6 מנות · נשארו 4" in reply
    assert "חלבון: 1 מתוך 5 מנות · נשארו 4" in reply
    assert "שומן: 4 מתוך 3 מנות · חריגה של 1 ⚠️" in reply
    assert "נשאר להיום: פחמימות 4 · חלבון 4" in reply
    claude.phrase_daily.assert_not_awaited()


async def test_today_with_nothing_logged(bot):
    await bot.text("/today")
    assert texts.NOTHING_LOGGED_TODAY in bot.request.texts()[-1]


def _log_previous_days(db, count: int, totals: dict[str, float]) -> None:
    for offset in range(1, count + 1):
        day = NOW - timedelta(days=offset)
        meal = make_meal(*[(key, key, portions) for key, portions in totals.items()])
        db.add_meal(meal, logged_at=day, day=day.date().isoformat())


async def test_week_combines_computed_numbers_with_claudes_note(bot, claude, db):
    _log_previous_days(db, 7, {"carbs": 6, "protein": 4, "fat": 3})
    db.add_meal(make_meal(("היום", "carbs", 1)), logged_at=NOW, day=TODAY)  # today is excluded

    await bot.text("/week")

    reply = bot.request.texts()[-1]
    assert "סיכום שבועי · 6.11 עד 12.11" in reply
    assert "ימים עם תיעוד: 7 מתוך 7" in reply
    assert "חלבון: ממוצע 4 ליום (יעד 5) · עמידה ביעד 80%" in reply
    assert reply.endswith("תובנות שבועיות מ-Claude")

    payload = claude.phrase_weekly.await_args.args[0]
    assert payload["first_day"] == "2026-11-06" and payload["last_day"] == "2026-11-12"
    assert TODAY not in payload["portions_per_day"]
    protein = next(c for c in payload["categories"] if c["key"] == "protein")
    assert protein["avg_per_logged_day"] == 4 and protein["days_under_target"] == 7


async def test_week_still_reports_numbers_when_claude_is_down(bot, claude, db):
    _log_previous_days(db, 5, {"carbs": 6, "protein": 2, "fat": 3})
    claude.phrase_weekly.side_effect = ClaudeError("connection")

    await bot.text("/week")

    reply = bot.request.texts()[-1]
    assert "ימים עם תיעוד: 5 מתוך 7" in reply
    assert "חלבון: מתחת ליעד ב-5 מתוך 5 ימים" in reply
    assert texts.CLAUDE_NOTE_UNAVAILABLE in reply


async def test_week_with_no_data(bot, claude):
    await bot.text("/week")
    assert bot.request.texts()[-1] == texts.NOTHING_LOGGED_WEEK
    claude.phrase_weekly.assert_not_awaited()


async def test_daily_report_uses_claude_only_for_the_note(bot, claude, db):
    reports = bot.handlers.reports
    db.add_meal(BREAD_AND_EGG, logged_at=NOW, day=TODAY)

    report = await reports.daily_report(NOW)

    assert "🌙 סיכום יומי · יום שישי 13.11" in report
    assert "פחמימות: 2 מתוך 6 מנות · נשארו 4" in report
    assert report.endswith("הערה יומית מ-Claude")
    payload = claude.phrase_daily.await_args.args[0]
    assert payload["categories"][0]["still_needed_to_reach_min"] == 4
    assert payload["meals"][0]["items"][0]["name"] == "2 פרוסות לחם"

    claude.phrase_daily.side_effect = ClaudeError("api")
    assert texts.CLAUDE_NOTE_UNAVAILABLE in await reports.daily_report(NOW)


async def test_daily_report_without_meals_skips_claude(bot, claude):
    report = await bot.handlers.reports.daily_report(NOW)
    assert texts.NOTHING_LOGGED_TODAY in report
    claude.phrase_daily.assert_not_awaited()


# ---- scheduling and misc -----------------------------------------------


async def test_summaries_are_scheduled_in_local_time(bot):
    jobs = {job.name: job for job in bot.app.job_queue.jobs()}
    assert set(jobs) == {DAILY_JOB, WEEKLY_JOB}

    def next_run(name, after):
        return jobs[name].job.trigger.get_next_fire_time(None, after)

    # NOW is Friday 14:30 in Israel: tonight at 21:00, and noon next Friday.
    assert next_run(DAILY_JOB, NOW) == datetime(2026, 11, 13, 21, 0, tzinfo=TZ)
    assert next_run(WEEKLY_JOB, NOW) == datetime(2026, 11, 20, 12, 0, tzinfo=TZ)
    # Earlier the same Friday, the weekly summary is still due today.
    assert next_run(WEEKLY_JOB, NOW.replace(hour=9)) == datetime(2026, 11, 13, 12, 0, tzinfo=TZ)
    # Summer time (UTC+3) keeps the same wall-clock hour.
    summer = datetime(2026, 7, 1, 8, 0, tzinfo=TZ)
    assert next_run(DAILY_JOB, summer) == datetime(2026, 7, 1, 21, 0, tzinfo=TZ)
    assert next_run(DAILY_JOB, summer).utcoffset() == timedelta(hours=3)


async def test_help_unknown_command_and_unsupported_messages(bot, claude):
    await bot.text("/start")
    assert bot.request.texts()[-1] == texts.HELP
    await bot.text("/nope")
    assert bot.request.texts()[-1] == texts.UNKNOWN_COMMAND
    await bot.sticker()
    assert bot.request.texts()[-1] == texts.UNSUPPORTED_MESSAGE
    claude.handle_message.assert_not_awaited()


async def test_placeholder_menu_is_called_out(bot, monkeypatch):
    from dataclasses import replace

    placeholder = replace(bot.handlers.menu, placeholder=True)
    monkeypatch.setattr(bot.handlers, "menu", placeholder)
    monkeypatch.setattr(bot.handlers.reports, "_menu", placeholder)

    await bot.text("/help")
    assert texts.PLACEHOLDER_WARNING in bot.request.texts()[-1]
    await bot.text("/today")
    assert texts.PLACEHOLDER_WARNING in bot.request.texts()[-1]
