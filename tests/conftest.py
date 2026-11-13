"""Shared fixtures: a small test menu, an in-memory DB and offline Telegram/Claude fakes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

from bisbot.app import build_application
from bisbot.claude_client import RouterResult
from bisbot.config import Settings
from bisbot.db import Database
from bisbot.menu import Category, Menu, load_menu
from bisbot.models import MealItem, ParsedMeal

TZ = ZoneInfo("Asia/Jerusalem")
ALLOWED_CHAT = 111
OTHER_CHAT = 999
# A Friday afternoon in Israel.
NOW = datetime(2026, 11, 13, 14, 30, tzinfo=TZ)


@pytest.fixture
def menu() -> Menu:
    return Menu(
        text="# Test plan\ncarbs 6, protein 5, fat 3 per day. Vegetables are free.",
        categories=(
            Category("carbs", "פחמימות", daily_min=6, daily_max=6),
            Category("protein", "חלבון", daily_min=5, daily_max=5),
            Category("fat", "שומן", daily_min=3, daily_max=3),
            Category("vegetables", "ירקות"),
        ),
    )


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def plan_menu() -> Menu:
    """The real plan shipped in the repository (menu.md + menu_targets.json)."""
    return load_menu(REPO_ROOT / "menu.md", REPO_ROOT / "menu_targets.json")


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="123456:TEST-TOKEN",
        anthropic_api_key="test-key",
        allowed_chat_id=ALLOWED_CHAT,
        tz=TZ,
    )


def make_meal(*items: tuple[str, str, float], meal_type: str = "lunch", notes: str = "") -> ParsedMeal:
    return ParsedMeal(
        meal_type=meal_type,
        items=[MealItem(name, category, portions) for name, category, portions in items],
        notes=notes,
    )


def log_result(meal: ParsedMeal) -> RouterResult:
    return RouterResult(intent="log", meal=meal)


class FakeClaude:
    """Stands in for ClaudeClient in handler tests."""

    def __init__(self) -> None:
        self.handle_message = AsyncMock()
        self.phrase_daily = AsyncMock(return_value="הערה יומית מ-Claude")
        self.phrase_weekly = AsyncMock(return_value="תובנות שבועיות מ-Claude")


@pytest.fixture
def claude() -> FakeClaude:
    return FakeClaude()


class OfflineRequest(BaseRequest):
    """Answers Telegram Bot API calls locally and records them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._message_id = 1000

    @property
    def read_timeout(self) -> float:
        return 1.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        if "/file/bot" in url:
            return 200, b"fake-jpeg-bytes"
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((endpoint, params))
        return 200, json.dumps({"ok": True, "result": self._result(endpoint, params)}).encode()

    def _result(self, endpoint: str, params: dict[str, Any]) -> Any:
        if endpoint == "getMe":
            return {"id": 42, "is_bot": True, "first_name": "BisBot", "username": "bisbot_test"}
        if endpoint in {"sendMessage", "editMessageText", "editMessageReplyMarkup"}:
            self._message_id += 1
            return {
                "message_id": params.get("message_id", self._message_id),
                "date": int(NOW.timestamp()),
                "chat": {"id": params.get("chat_id", ALLOWED_CHAT), "type": "private"},
                "text": params.get("text", ""),
            }
        if endpoint == "getFile":
            return {
                "file_id": params["file_id"],
                "file_unique_id": "unique",
                "file_path": "photos/file_1.jpg",
            }
        return True

    # ---- assertions helpers ----

    def sent(self, endpoint: str = "sendMessage") -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == endpoint]

    def texts(self) -> list[str]:
        return [params["text"] for params in self.sent()]

    def last_buttons(self) -> list[dict[str, str]]:
        """Buttons of the most recent message that had an inline keyboard."""
        for params in reversed(self.sent()):
            markup = params.get("reply_markup")
            if markup:
                if isinstance(markup, str):
                    markup = json.loads(markup)
                return [button for row in markup["inline_keyboard"] for button in row]
        return []


class Bot:
    """Drives a real Application with fake updates; nothing touches the network."""

    def __init__(self, application, request: OfflineRequest):
        self.app = application
        self.request = request
        self._update_id = 0
        self._message_id = 0

    @property
    def handlers(self):
        return self.app.bot_data["handlers"]

    async def _process(self, payload: dict[str, Any]) -> None:
        self._update_id += 1
        payload["update_id"] = self._update_id
        await self.app.process_update(Update.de_json(payload, self.app.bot))

    def _message(self, chat_id: int, **fields: Any) -> dict[str, Any]:
        self._message_id += 1
        return {
            "message_id": self._message_id,
            "date": int(NOW.timestamp()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "User"},
            **fields,
        }

    async def text(self, text: str, chat_id: int = ALLOWED_CHAT) -> None:
        fields: dict[str, Any] = {"text": text}
        if text.startswith("/"):
            command = text.split()[0]
            fields["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        await self._process({"message": self._message(chat_id, **fields)})

    async def photo(self, caption: str | None = None, chat_id: int = ALLOWED_CHAT) -> None:
        fields: dict[str, Any] = {
            "photo": [
                {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90},
                {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 960},
            ]
        }
        if caption:
            fields["caption"] = caption
        await self._process({"message": self._message(chat_id, **fields)})

    async def sticker(self, chat_id: int = ALLOWED_CHAT) -> None:
        sticker = {
            "file_id": "st",
            "file_unique_id": "st",
            "type": "regular",
            "width": 512,
            "height": 512,
            "is_animated": False,
            "is_video": False,
        }
        await self._process({"message": self._message(chat_id, sticker=sticker)})

    async def click(self, data: str, chat_id: int = ALLOWED_CHAT) -> None:
        await self._process(
            {
                "callback_query": {
                    "id": "cb",
                    "from": {"id": chat_id, "is_bot": False, "first_name": "User"},
                    "chat_instance": "ci",
                    "data": data,
                    "message": self._message(chat_id, text="previous bot message"),
                }
            }
        )

    async def click_button(self, label_prefix: str) -> None:
        """Click the button whose label starts with the given emoji/text."""
        for button in self.request.last_buttons():
            if button["text"].startswith(label_prefix):
                await self.click(button["callback_data"])
                return
        raise AssertionError(f"No button starting with {label_prefix!r}")


@pytest.fixture
async def bot(settings, db, menu, claude) -> Bot:
    request = OfflineRequest()
    builder = ApplicationBuilder().request(request).get_updates_request(OfflineRequest())
    application = build_application(settings, db, menu, claude, builder=builder)
    application.bot_data["handlers"]._clock = lambda: NOW
    await application.initialize()
    yield Bot(application, request)
    await application.shutdown()


def fake_response(*blocks: Any, stop_reason: str = "end_turn") -> SimpleNamespace:
    """A minimal stand-in for an Anthropic Message."""
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model="claude-test",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


def tool_use(name: str, payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=payload, id="toolu_1")


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)
