"""Builds the Telegram application and runs it in polling mode."""

from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .claude_client import ClaudeClient
from .config import ConfigError, Settings, load_settings
from .db import Database
from .handlers import BotHandlers, make_chat_gate, on_error
from .jobs import schedule_jobs
from .menu import Menu, MenuError, load_menu
from .reports import Reports

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("today", "מה אכלתי היום ומה נשאר"),
    BotCommand("week", "סיכום 7 הימים האחרונים"),
    BotCommand("undo", "מחיקת הארוחה האחרונה"),
    BotCommand("save", "שמירת הארוחה האחרונה כקיצור"),
    BotCommand("shortcuts", "רשימת הקיצורים"),
    BotCommand("delshortcut", "מחיקת קיצור"),
    BotCommand("help", "עזרה"),
]


def build_application(
    settings: Settings,
    db: Database,
    menu: Menu,
    claude: ClaudeClient,
    builder: ApplicationBuilder | None = None,
) -> Application:
    reports = Reports(db, menu, claude)
    handlers = BotHandlers(
        settings=settings, db=db, menu=menu, claude=claude, reports=reports
    )

    async def post_init(application: Application) -> None:
        await application.bot.set_my_commands(BOT_COMMANDS)

    builder = builder or ApplicationBuilder()
    application = builder.token(settings.telegram_bot_token).post_init(post_init).build()

    # Group -1 runs before everything else: updates from other chats stop here.
    application.add_handler(
        TypeHandler(Update, make_chat_gate(settings.allowed_chat_id)), group=-1
    )

    application.add_handler(CommandHandler(["start", "help"], handlers.cmd_help))
    application.add_handler(CommandHandler("today", handlers.cmd_today))
    application.add_handler(CommandHandler("week", handlers.cmd_week))
    application.add_handler(CommandHandler("undo", handlers.cmd_undo))
    application.add_handler(CommandHandler("save", handlers.cmd_save))
    application.add_handler(CommandHandler("shortcuts", handlers.cmd_shortcuts))
    application.add_handler(CommandHandler("delshortcut", handlers.cmd_delshortcut))
    application.add_handler(MessageHandler(filters.COMMAND, handlers.cmd_unknown))
    application.add_handler(CallbackQueryHandler(handlers.on_callback))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handlers.on_photo)
    )
    application.add_handler(MessageHandler(filters.TEXT, handlers.on_text))
    application.add_handler(MessageHandler(filters.ALL, handlers.on_unsupported))
    application.add_error_handler(on_error)

    schedule_jobs(application, settings, reports)
    application.bot_data["handlers"] = handlers
    return application


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        menu = load_menu(settings.menu_path, settings.targets_path)
    except MenuError as exc:
        raise SystemExit(f"Menu error: {exc}") from None
    if menu.placeholder:
        logger.warning("The menu is marked as a placeholder - replace it with the real plan")
    db = Database(settings.db_path)
    claude = ClaudeClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
        menu=menu,
        effort=settings.claude_effort,
        refusal_fallback=settings.claude_refusal_fallback,
    )
    application = build_application(settings, db, menu, claude)
    logger.info(
        "Starting BisBot (model %s, timezone %s, db %s)",
        settings.claude_model,
        settings.tz,
        settings.db_path,
    )
    try:
        application.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    finally:
        db.close()
