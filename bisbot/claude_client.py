"""All communication with the Claude API.

Two kinds of calls:

* ``handle_message`` - classifies an incoming message and either parses it into
  a structured meal (tool call ``log_meal``) or answers it (``answer_user``).
* ``phrase_daily`` / ``phrase_weekly`` - turn numbers computed elsewhere into a
  short Hebrew note.

Every failure surfaces as ``ClaudeError`` with a ``kind`` the handlers map to a
Hebrew message.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from . import prompts
from .menu import Menu
from .models import ParsedMeal
from .parsing import MealValidationError, validate_meal

logger = logging.getLogger(__name__)

# Generous cap: thinking counts toward it, and a truncated tool call is useless.
MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 90.0
FALLBACK_BETA = "server-side-fallback-2026-07-01"

INTENT_LOG = "log"
INTENT_QUESTION = "question"
INTENT_OTHER = "other"


class ClaudeError(Exception):
    """A Claude request failed. ``kind`` selects the message shown to the user."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


@dataclass
class RouterResult:
    intent: str  # log | question | other
    meal: ParsedMeal | None = None
    answer: str | None = None


class ClaudeClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        menu: Menu,
        effort: str | None = "low",
        refusal_fallback: bool = True,
        client: Any | None = None,
    ):
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1
        )
        self._model = model
        self._menu = menu
        self._effort = effort
        self._refusal_fallback = refusal_fallback
        self._router_system = _cached_system(prompts.router_system_prompt(menu))
        self._router_tools = prompts.router_tools(menu)
        self._summary_system = _cached_system(prompts.summary_system_prompt(menu))

    # ---- public API ----------------------------------------------------

    async def handle_message(
        self,
        *,
        text: str,
        context: str,
        image: bytes | None = None,
        image_media_type: str = "image/jpeg",
        previous: ParsedMeal | None = None,
        correction: str | None = None,
    ) -> RouterResult:
        """Classify a message and return either a parsed meal or an answer.

        Pass ``previous`` and ``correction`` to re-parse a meal the user asked
        to fix; ``text``/``image`` are then the original message.
        """
        content: list[dict[str, Any]] = []
        if image is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": base64.standard_b64encode(image).decode("ascii"),
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": prompts.user_turn_text(
                    context=context,
                    text=text,
                    has_image=image is not None,
                    previous=previous,
                    correction=correction,
                ),
            }
        )

        # The tool call is steered by the prompt (current models reject a forced
        # tool_choice), so an unusable call gets one corrective retry.
        last_error = ""
        for _attempt in range(2):
            response = await self._create(
                system=self._router_system,
                tools=self._router_tools,
                messages=[{"role": "user", "content": content}],
            )
            try:
                return self._interpret(response)
            except MealValidationError as exc:
                last_error = str(exc)
                logger.warning("Claude returned an unusable log_meal call: %s", exc)
                content = content + [
                    {"type": "text", "text": prompts.invalid_call_feedback(last_error)}
                ]
        raise ClaudeError("invalid_output", last_error)

    async def phrase_daily(self, payload: dict[str, Any]) -> str:
        return await self._phrase(prompts.DAILY_NOTE_REQUEST, payload)

    async def phrase_weekly(self, payload: dict[str, Any]) -> str:
        return await self._phrase(prompts.WEEKLY_NOTE_REQUEST, payload)

    # ---- internals -----------------------------------------------------

    async def _phrase(self, template: str, payload: dict[str, Any]) -> str:
        request = template.format(payload=json.dumps(payload, ensure_ascii=False, indent=2))
        response = await self._create(
            system=self._summary_system,
            messages=[{"role": "user", "content": request}],
        )
        text = _text_of(response)
        if not text:
            raise ClaudeError("invalid_output", "empty summary text")
        return text

    def _interpret(self, response: Any) -> RouterResult:
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if block.name == prompts.LOG_MEAL_TOOL:
                return RouterResult(
                    intent=INTENT_LOG, meal=validate_meal(block.input, self._menu)
                )
            if block.name == prompts.ANSWER_TOOL:
                payload = block.input if isinstance(block.input, dict) else {}
                answer = str(payload.get("text") or "").strip()
                if not answer:
                    raise ClaudeError("invalid_output", "empty answer")
                intent = payload.get("intent")
                return RouterResult(
                    intent=intent if intent == INTENT_QUESTION else INTENT_OTHER,
                    answer=answer,
                )
        # No tool call: a plain-text reply is still a usable answer.
        text = _text_of(response)
        if text:
            return RouterResult(intent=INTENT_OTHER, answer=text)
        raise ClaudeError("invalid_output", "no tool call and no text")

    async def _create(self, **params: Any) -> Any:
        request: dict[str, Any] = {"model": self._model, "max_tokens": MAX_TOKENS, **params}
        if self._effort:
            request["output_config"] = {"effort": self._effort}
        try:
            if self._refusal_fallback:
                # If the model declines a request, the API re-runs it on the
                # model's default fallback inside the same call.
                response = await self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise ClaudeError("auth", str(exc)) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ClaudeError("auth", str(exc)) from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("rate_limit", str(exc)) from exc
        except anthropic.APIStatusError as exc:
            logger.error(
                "Claude API error %s (request %s): %s",
                exc.status_code,
                getattr(exc, "request_id", None),
                exc,
            )
            raise ClaudeError("api", str(exc)) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise ClaudeError("connection", str(exc)) from exc

        usage = getattr(response, "usage", None)
        logger.info(
            "Claude %s stop=%s in=%s out=%s cache_read=%s",
            getattr(response, "model", self._model),
            response.stop_reason,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "cache_read_input_tokens", None),
        )
        if response.stop_reason == "refusal":
            raise ClaudeError("refusal")
        if response.stop_reason == "max_tokens":
            raise ClaudeError("invalid_output", "response was cut off at max_tokens")
        return response


def _cached_system(text: str) -> list[dict[str, Any]]:
    """System prompt as one cacheable block (it is identical on every request)."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _text_of(response: Any) -> str:
    return "\n".join(
        block.text.strip()
        for block in response.content
        if getattr(block, "type", None) == "text" and block.text.strip()
    ).strip()
