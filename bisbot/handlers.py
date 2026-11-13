"""Telegram handlers: commands, meal logging flow, Q&A and inline buttons.

State that must survive between messages lives in the ``pending`` table, as a
JSON object with two independent slots per chat:

* ``meal``  - a parsed meal waiting for Confirm / Fix / Cancel.
* ``retry`` - the raw input of a request that failed, so it can be re-sent.
"""

from __future__ import annotations

import logging
import secrets
from collections import deque
from datetime import datetime
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from . import texts
from .claude_client import INTENT_LOG, ClaudeClient, ClaudeError
from .config import Settings
from .db import Database, normalize_shortcut_name
from .menu import Menu
from .models import ParsedMeal
from .parsing import MealValidationError, validate_meal
from .reports import Reports

logger = logging.getLogger(__name__)

CB_CONFIRM, CB_FIX, CB_CANCEL, CB_RETRY = "c", "f", "x", "r"
TELEGRAM_MESSAGE_LIMIT = 4000
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 3_500_000  # stays under the API's image size limit after base64
HISTORY_TURNS = 8
MAX_SHORTCUT_NAME = 50

Context = ContextTypes.DEFAULT_TYPE


def make_chat_gate(allowed_chat_id: int) -> Callable[[Update, Context], Any]:
    """A first-in-line handler that drops every update from any other chat."""

    async def gate(update: Update, context: Context) -> None:
        chat = update.effective_chat
        if chat is None or chat.id != allowed_chat_id:
            logger.info("Ignoring update from chat %s", chat.id if chat else None)
            raise ApplicationHandlerStop

    return gate


def _meal_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(texts.BTN_CONFIRM, callback_data=f"{CB_CONFIRM}:{token}"),
                InlineKeyboardButton(texts.BTN_FIX, callback_data=f"{CB_FIX}:{token}"),
                InlineKeyboardButton(texts.BTN_CANCEL, callback_data=f"{CB_CANCEL}:{token}"),
            ]
        ]
    )


def _retry_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(texts.BTN_RETRY, callback_data=f"{CB_RETRY}:{token}")]]
    )


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split a long text on line breaks so each part fits in one message."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current)
    return parts or [text[:limit]]


class BotHandlers:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        menu: Menu,
        claude: ClaudeClient,
        reports: Reports,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.db = db
        self.menu = menu
        self.claude = claude
        self.reports = reports
        self._clock = clock or (lambda: datetime.now(settings.tz))
        # Short in-memory memory of the chat so follow-up questions make sense.
        self._history: deque[tuple[str, str]] = deque(maxlen=HISTORY_TURNS)

    def now(self) -> datetime:
        return self._clock()

    # ---- commands ------------------------------------------------------

    async def cmd_help(self, update: Update, context: Context) -> None:
        text = texts.HELP
        if self.menu.placeholder:
            text += f"\n\n{texts.PLACEHOLDER_WARNING}"
        await self._send(context, update.effective_chat.id, text)

    async def cmd_today(self, update: Update, context: Context) -> None:
        await self._send(context, update.effective_chat.id, self.reports.today_text(self.now()))

    async def cmd_week(self, update: Update, context: Context) -> None:
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await self._send(context, chat_id, await self.reports.weekly_report(self.now()))

    async def cmd_undo(self, update: Update, context: Context) -> None:
        chat_id = update.effective_chat.id
        last = self.db.last_meal()
        if last is None or not self.db.delete_meal(last.id):
            await self._send(context, chat_id, texts.UNDO_NOTHING)
            return
        logger.info("Undid meal %s", last.id)
        parts = [texts.UNDO_DONE, texts.format_meal(last.meal, self.menu, show_confidence=False)]
        balance = self.reports.after_save_text(self.now())
        if balance:
            parts.append(balance)
        await self._send(context, chat_id, "\n\n".join(parts))

    async def cmd_save(self, update: Update, context: Context) -> None:
        chat_id = update.effective_chat.id
        name = normalize_shortcut_name(" ".join(context.args or []))
        if not name:
            await self._send(context, chat_id, texts.SAVE_USAGE)
            return
        if name.startswith("/") or len(name) > MAX_SHORTCUT_NAME:
            await self._send(context, chat_id, texts.SAVE_BAD_NAME)
            return
        last = self.db.last_meal()
        if last is None:
            await self._send(context, chat_id, texts.SAVE_NO_MEAL)
            return
        self.db.save_shortcut(name, last.meal)
        await self._send(
            context,
            chat_id,
            f"{texts.shortcut_saved(name)}\n\n"
            f"{texts.format_meal(last.meal, self.menu, show_confidence=False)}",
        )

    async def cmd_shortcuts(self, update: Update, context: Context) -> None:
        shortcuts = self.db.list_shortcuts()
        if not shortcuts:
            await self._send(context, update.effective_chat.id, texts.SHORTCUTS_EMPTY)
            return
        lines = [texts.SHORTCUTS_TITLE]
        lines += [texts.format_shortcut_line(name, payload, self.menu) for name, payload in shortcuts]
        await self._send(context, update.effective_chat.id, "\n".join(lines))

    async def cmd_delshortcut(self, update: Update, context: Context) -> None:
        chat_id = update.effective_chat.id
        name = normalize_shortcut_name(" ".join(context.args or []))
        if not name:
            await self._send(context, chat_id, texts.DELSHORTCUT_USAGE)
        elif self.db.delete_shortcut(name):
            await self._send(context, chat_id, texts.shortcut_deleted(name))
        else:
            await self._send(context, chat_id, texts.shortcut_not_found(name))

    async def cmd_unknown(self, update: Update, context: Context) -> None:
        await self._send(context, update.effective_chat.id, texts.UNKNOWN_COMMAND)

    # ---- incoming messages ---------------------------------------------

    async def on_text(self, update: Update, context: Context) -> None:
        message = update.effective_message
        text = (message.text or "").strip()
        if not text:
            return
        chat_id = message.chat_id
        meal_slot = self.db.get_pending(chat_id).get("meal")

        if meal_slot and meal_slot.get("awaiting_fix"):
            await self._process(
                chat_id,
                context,
                text=meal_slot.get("raw_text") or "",
                photo_file_id=meal_slot.get("photo_file_id"),
                photo_mime=meal_slot.get("photo_mime"),
                correction=text,
            )
            return

        shortcut = self.db.get_shortcut(text)
        if shortcut is not None:
            await self._log_shortcut(chat_id, context, normalize_shortcut_name(text), shortcut)
            return

        await self._process(chat_id, context, text=text)

    async def on_photo(self, update: Update, context: Context) -> None:
        message = update.effective_message
        chat_id = message.chat_id
        if message.photo:
            file_id, mime = message.photo[-1].file_id, "image/jpeg"
        else:
            document = message.document
            mime = (document.mime_type or "").lower()
            if mime not in SUPPORTED_IMAGE_TYPES:
                await self._send(context, chat_id, texts.UNSUPPORTED_MESSAGE)
                return
            if (document.file_size or 0) > MAX_IMAGE_BYTES:
                await self._send(context, chat_id, texts.IMAGE_TOO_LARGE)
                return
            file_id = document.file_id
        caption = (message.caption or "").strip()

        meal_slot = self.db.get_pending(chat_id).get("meal")
        if meal_slot and meal_slot.get("awaiting_fix"):
            # A photo sent while fixing replaces the original one.
            await self._process(
                chat_id,
                context,
                text=meal_slot.get("raw_text") or "",
                photo_file_id=file_id,
                photo_mime=mime,
                correction=caption or "(see the new photo)",
            )
            return
        await self._process(chat_id, context, text=caption, photo_file_id=file_id, photo_mime=mime)

    async def on_unsupported(self, update: Update, context: Context) -> None:
        await self._send(context, update.effective_chat.id, texts.UNSUPPORTED_MESSAGE)

    # ---- inline buttons ------------------------------------------------

    async def on_callback(self, update: Update, context: Context) -> None:
        query = update.callback_query
        chat_id = update.effective_chat.id
        action, _, token = (query.data or "").partition(":")
        pending = self.db.get_pending(chat_id)

        if action == CB_RETRY:
            retry = pending.get("retry")
            if not retry or retry.get("token") != token:
                await self._stale(query)
                return
            await query.answer(texts.RETRYING)
            await self._remove_keyboard(query)
            await self._process(
                chat_id,
                context,
                text=retry.get("raw_text") or "",
                photo_file_id=retry.get("photo_file_id"),
                photo_mime=retry.get("photo_mime"),
                correction=retry.get("correction"),
            )
            return

        meal_slot = pending.get("meal")
        if not meal_slot or meal_slot.get("token") != token:
            await self._stale(query)
            return

        if action == CB_CONFIRM:
            await self._confirm(query, chat_id, pending, meal_slot)
        elif action == CB_FIX:
            meal_slot["awaiting_fix"] = True
            self.db.set_pending(chat_id, pending)
            await query.answer()
            await self._send(context, chat_id, texts.ASK_FIX)
        elif action == CB_CANCEL:
            pending.pop("meal", None)
            self.db.set_pending(chat_id, pending)
            await query.answer()
            meal_text = self._slot_meal_text(meal_slot)
            await self._edit(query, f"{meal_text}\n\n{texts.CANCELLED}".strip())
        else:
            await self._stale(query)

    async def _confirm(self, query, chat_id: int, pending: dict, meal_slot: dict) -> None:
        try:
            meal = validate_meal(meal_slot.get("parsed"), self.menu)
        except MealValidationError:
            logger.exception("Pending meal no longer validates; dropping it")
            pending.pop("meal", None)
            self.db.set_pending(chat_id, pending)
            await self._stale(query)
            return
        now = self.now()
        raw_text = meal_slot.get("raw_text") or ""
        for correction in meal_slot.get("corrections", []):
            raw_text += f"\n[תיקון] {correction}"
        meal_id = self.db.add_meal(
            meal,
            logged_at=now,
            day=now.date().isoformat(),
            raw_text=raw_text or None,
            photo_file_id=meal_slot.get("photo_file_id"),
        )
        pending.pop("meal", None)
        self.db.set_pending(chat_id, pending)
        logger.info("Saved meal %s (%s items)", meal_id, len(meal.items))
        self._history.append(("bot", f"[meal saved: {self._meal_brief(meal)}]"))

        await query.answer(texts.SAVED)
        parts = [texts.format_meal(meal, self.menu, show_confidence=False), texts.SAVED]
        balance = self.reports.after_save_text(now, meal)
        if balance:
            parts.append(balance)
        await self._edit(query, "\n\n".join(parts))

    # ---- core flow -----------------------------------------------------

    async def _process(
        self,
        chat_id: int,
        context: Context,
        *,
        text: str,
        photo_file_id: str | None = None,
        photo_mime: str | None = None,
        correction: str | None = None,
    ) -> None:
        """Send one input to Claude and act on the result.

        With ``correction`` set, ``text``/photo are the original message of the
        pending meal and Claude re-parses it with the previous result as context.
        """
        pending = self.db.get_pending(chat_id)
        meal_slot = pending.get("meal")
        previous: ParsedMeal | None = None
        if correction is not None:
            try:
                previous = validate_meal((meal_slot or {}).get("parsed"), self.menu)
            except MealValidationError:
                # Nothing left to fix: treat the correction as a new message.
                text, correction = correction, None

        self._history.append(("user", correction or text or "[photo]"))
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
            image = await self._download(context, photo_file_id) if photo_file_id else None
            result = await self.claude.handle_message(
                text=text,
                context=self.reports.claude_context(self.now(), list(self._history)[:-1]),
                image=image,
                image_media_type=photo_mime or "image/jpeg",
                previous=previous,
                correction=correction,
            )
        except (ClaudeError, TelegramError) as exc:
            kind = exc.kind if isinstance(exc, ClaudeError) else "telegram"
            logger.error("Request failed (%s): %s", kind, exc, exc_info=True)
            self._history.pop()
            # Keep the raw input so nothing has to be typed again.
            token = secrets.token_hex(4)
            pending["retry"] = {
                "token": token,
                "raw_text": text,
                "photo_file_id": photo_file_id,
                "photo_mime": photo_mime,
                "correction": correction,
            }
            self.db.set_pending(chat_id, pending)
            await self._send(context, chat_id, texts.claude_error(kind), _retry_keyboard(token))
            return

        pending.pop("retry", None)
        if result.intent == INTENT_LOG and result.meal is not None:
            token = secrets.token_hex(4)
            corrections = list((meal_slot or {}).get("corrections", [])) if previous else []
            if correction:
                corrections.append(correction)
            pending["meal"] = {
                "token": token,
                "raw_text": text,
                "photo_file_id": photo_file_id,
                "photo_mime": photo_mime,
                "parsed": result.meal.to_dict(),
                "awaiting_fix": False,
                "corrections": corrections,
            }
            self.db.set_pending(chat_id, pending)
            self._history.append(("bot", f"[parsed a meal: {self._meal_brief(result.meal)}]"))
            await self._send(
                context, chat_id, texts.format_meal(result.meal, self.menu), _meal_keyboard(token)
            )
            return

        answer = result.answer or texts.UNEXPECTED_ERROR
        self._history.append(("bot", answer))
        if previous is not None and meal_slot:
            # The "correction" turned out to be a question; the meal still waits.
            meal_slot["awaiting_fix"] = False
            self.db.set_pending(chat_id, pending)
            await self._send(context, chat_id, answer)
            await self._send(
                context,
                chat_id,
                f"{texts.PENDING_REMINDER}\n\n{texts.format_meal(previous, self.menu)}",
                _meal_keyboard(meal_slot["token"]),
            )
            return
        self.db.set_pending(chat_id, pending)
        await self._send(context, chat_id, answer)

    async def _log_shortcut(
        self, chat_id: int, context: Context, name: str, payload: dict[str, Any]
    ) -> None:
        """Log a saved meal directly - no Claude call, no confirmation step."""
        try:
            meal = validate_meal(payload, self.menu)
        except MealValidationError:
            logger.warning("Shortcut %r does not match the current menu", name)
            await self._send(context, chat_id, texts.SHORTCUT_INVALID.format(name=name))
            return
        now = self.now()
        meal_id = self.db.add_meal(
            meal, logged_at=now, day=now.date().isoformat(), raw_text=name
        )
        logger.info("Logged shortcut %r as meal %s", name, meal_id)
        self._history.append(("user", name))
        self._history.append(("bot", f"[meal saved: {self._meal_brief(meal)}]"))
        parts = [
            texts.shortcut_logged(name),
            texts.format_meal(meal, self.menu, show_confidence=False),
        ]
        balance = self.reports.after_save_text(now, meal)
        if balance:
            parts.append(balance)
        await self._send(context, chat_id, "\n\n".join(parts))

    # ---- helpers -------------------------------------------------------

    def _meal_brief(self, meal: ParsedMeal) -> str:
        items = ", ".join(f"{i.name} ({i.category} {i.portions:g})" for i in meal.items)
        return f"{meal.meal_type}: {items}"

    def _slot_meal_text(self, meal_slot: dict) -> str:
        try:
            meal = validate_meal(meal_slot.get("parsed"), self.menu)
        except MealValidationError:
            return ""
        return texts.format_meal(meal, self.menu, show_confidence=False)

    async def _download(self, context: Context, file_id: str) -> bytes:
        telegram_file = await context.bot.get_file(file_id)
        return bytes(await telegram_file.download_as_bytearray())

    async def _send(
        self,
        context: Context,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        parts = split_message(text)
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            await context.bot.send_message(
                chat_id=chat_id, text=part, reply_markup=reply_markup if is_last else None
            )

    async def _edit(self, query, text: str) -> None:
        """Replace the button message's text (which also removes its buttons)."""
        try:
            await query.edit_message_text(text[:TELEGRAM_MESSAGE_LIMIT])
        except BadRequest as exc:
            logger.warning("Could not edit message: %s", exc)

    async def _remove_keyboard(self, query) -> None:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest as exc:
            logger.debug("Could not remove keyboard: %s", exc)

    async def _stale(self, query) -> None:
        await query.answer(texts.STALE_BUTTON)
        await self._remove_keyboard(query)


async def on_error(update: object, context: Context) -> None:
    """Log every unhandled exception and, when possible, tell the user."""
    logger.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=texts.UNEXPECTED_ERROR
            )
        except TelegramError:
            logger.exception("Could not report the error to the user")
