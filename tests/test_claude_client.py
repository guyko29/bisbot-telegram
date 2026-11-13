"""ClaudeClient against a mocked Anthropic SDK client - no network involved."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from bisbot import prompts
from bisbot.claude_client import FALLBACK_BETA, ClaudeClient, ClaudeError

from .conftest import fake_response, make_meal, text_block, tool_use

MEAL_INPUT = {
    "meal_type": "breakfast",
    "items": [
        {"name": "2 פרוסות לחם", "category": "carbs", "portions": 2, "confidence": 0.95},
        {"name": "ביצה", "category": "protein", "portions": 1, "confidence": 0.9},
    ],
    "notes": "",
}


def make_client(menu, *responses, refusal_fallback=True, effort="low"):
    create = AsyncMock(side_effect=list(responses))
    sdk = SimpleNamespace(
        messages=SimpleNamespace(create=create),
        beta=SimpleNamespace(messages=SimpleNamespace(create=create)),
    )
    client = ClaudeClient(
        api_key="k",
        model="claude-sonnet-5-5",
        menu=menu,
        effort=effort,
        refusal_fallback=refusal_fallback,
        client=sdk,
    )
    return client, create


def request_text(create, call=0) -> str:
    content = create.call_args_list[call].kwargs["messages"][0]["content"]
    return "\n".join(block["text"] for block in content if block["type"] == "text")


async def test_meal_report_becomes_a_validated_meal(menu):
    client, create = make_client(menu, fake_response(tool_use("log_meal", MEAL_INPUT), stop_reason="tool_use"))

    result = await client.handle_message(text="2 פרוסות לחם וביצה", context="CTX")

    assert result.intent == "log" and result.answer is None
    assert result.meal.totals() == {"carbs": 2.0, "protein": 1.0}

    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["betas"] == [FALLBACK_BETA] and kwargs["fallbacks"] == "default"
    # The call is steered by the prompt, never forced: current models reject that.
    assert "tool_choice" not in kwargs
    assert "<context>\nCTX\n</context>" in request_text(create)
    assert "<user_message>\n2 פרוסות לחם וביצה\n</user_message>" in request_text(create)


async def test_system_prompt_holds_the_menu_and_is_cacheable(menu):
    client, create = make_client(menu, fake_response(tool_use("log_meal", MEAL_INPUT)))
    await client.handle_message(text="x", context="c")

    (system,) = create.call_args.kwargs["system"]
    assert menu.text in system["text"]
    assert "carbs: פחמימות (daily target 6)" in system["text"]
    assert "vegetables: ירקות (tracked, no target)" in system["text"]
    assert system["cache_control"] == {"type": "ephemeral"}
    # Volatile context must stay out of the cached prefix.
    assert "CTX" not in system["text"]


async def test_tool_schema_only_allows_menu_categories(menu):
    log_tool, answer_tool = prompts.router_tools(menu)
    schema = log_tool["input_schema"]
    item = schema["properties"]["items"]["items"]

    assert log_tool["strict"] is True and answer_tool["strict"] is True
    assert item["properties"]["category"]["enum"] == ["carbs", "protein", "fat", "vegetables"]
    assert schema["properties"]["meal_type"]["enum"] == menu.meal_type_keys
    assert set(item["required"]) == {"name", "category", "portions", "confidence"}
    assert schema["additionalProperties"] is False and item["additionalProperties"] is False


async def test_question_is_answered_not_logged(menu):
    client, _ = make_client(
        menu,
        fake_response(tool_use("answer_user", {"intent": "question", "text": " אפשר חביתה וסלט "})),
    )
    result = await client.handle_message(text="מה לאכול בערב?", context="c")
    assert (result.intent, result.meal, result.answer) == ("question", None, "אפשר חביתה וסלט")


async def test_plain_text_reply_is_used_as_an_answer(menu):
    client, _ = make_client(menu, fake_response(text_block("שלום!")))
    result = await client.handle_message(text="היי", context="c")
    assert (result.intent, result.answer) == ("other", "שלום!")


async def test_photo_is_sent_as_base64_before_the_text(menu):
    client, create = make_client(menu, fake_response(tool_use("log_meal", MEAL_INPUT)))
    await client.handle_message(text="", context="c", image=b"\xff\xd8jpeg", image_media_type="image/png")

    image, text = create.call_args.kwargs["messages"][0]["content"]
    assert image["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": base64.standard_b64encode(b"\xff\xd8jpeg").decode(),
    }
    assert "The attached photo belongs to this message." in text["text"]


async def test_fix_sends_previous_parse_and_correction(menu):
    client, create = make_client(menu, fake_response(tool_use("log_meal", MEAL_INPUT)))
    previous = make_meal(("פרוסת לחם", "carbs", 1), meal_type="breakfast")

    await client.handle_message(
        text="לחם וביצה", context="c", previous=previous, correction="זה היה 2 פרוסות"
    )

    text = request_text(create)
    assert "<original_message>\nלחם וביצה\n</original_message>" in text
    assert "<correction>\nזה היה 2 פרוסות\n</correction>" in text
    sent_previous = text.split("<previous_parse>\n")[1].split("\n</previous_parse>")[0]
    assert json.loads(sent_previous) == previous.to_dict()


async def test_invalid_tool_call_is_retried_once_with_feedback(menu):
    bad = {**MEAL_INPUT, "items": []}
    client, create = make_client(
        menu,
        fake_response(tool_use("log_meal", bad)),
        fake_response(tool_use("log_meal", MEAL_INPUT)),
    )
    result = await client.handle_message(text="לחם", context="c")

    assert result.intent == "log" and create.await_count == 2
    assert "could not be used: items must be a non-empty list" in request_text(create, call=1)


async def test_two_invalid_tool_calls_give_up(menu):
    bad = {**MEAL_INPUT, "items": []}
    client, create = make_client(
        menu, fake_response(tool_use("log_meal", bad)), fake_response(tool_use("log_meal", bad))
    )
    with pytest.raises(ClaudeError) as error:
        await client.handle_message(text="לחם", context="c")
    assert error.value.kind == "invalid_output" and create.await_count == 2


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (fake_response(stop_reason="refusal"), "refusal"),
        (fake_response(text_block("חצי תשו"), stop_reason="max_tokens"), "invalid_output"),
        (fake_response(), "invalid_output"),
        (fake_response(tool_use("answer_user", {"intent": "other", "text": " "})), "invalid_output"),
    ],
)
async def test_unusable_responses_raise(menu, response, kind):
    client, _ = make_client(menu, response)
    with pytest.raises(ClaudeError) as error:
        await client.handle_message(text="x", context="c")
    assert error.value.kind == kind


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_status_error(anthropic.RateLimitError, 429), "rate_limit"),
        (_status_error(anthropic.AuthenticationError, 401), "auth"),
        (_status_error(anthropic.InternalServerError, 500), "api"),
        (_status_error(anthropic.BadRequestError, 400), "api"),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "connection",
        ),
        (
            anthropic.APITimeoutError(
                request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            ),
            "connection",
        ),
    ],
)
async def test_api_errors_are_mapped_to_kinds(menu, error, kind):
    client, _ = make_client(menu, error)
    with pytest.raises(ClaudeError) as raised:
        await client.handle_message(text="x", context="c")
    assert raised.value.kind == kind


async def test_fallback_and_effort_can_be_switched_off(menu):
    client, create = make_client(
        menu, fake_response(tool_use("log_meal", MEAL_INPUT)), refusal_fallback=False, effort=None
    )
    await client.handle_message(text="x", context="c")
    kwargs = create.call_args.kwargs
    assert "betas" not in kwargs and "fallbacks" not in kwargs and "output_config" not in kwargs


async def test_summary_note_gets_numbers_and_no_tools(menu):
    client, create = make_client(menu, fake_response(text_block(" שבוע טוב, חסר קצת חלבון. ")))

    note = await client.phrase_weekly({"overall_adherence_pct": 81.5, "categories": []})

    assert note == "שבוע טוב, חסר קצת חלבון."
    kwargs = create.call_args.kwargs
    assert "tools" not in kwargs
    assert '"overall_adherence_pct": 81.5' in kwargs["messages"][0]["content"]
    assert menu.text in kwargs["system"][0]["text"]


async def test_empty_summary_note_raises(menu):
    client, _ = make_client(menu, fake_response())
    with pytest.raises(ClaudeError):
        await client.phrase_daily({"categories": []})


async def test_real_plan_prompt_states_targets_rules_and_extras(plan_menu):
    system = prompts.router_system_prompt(plan_menu)

    assert plan_menu.text in system
    assert "- carbs: פחמימות (daily target 3 to 5)" in system
    assert "- protein: חלבון (daily target 3)" in system
    assert "- vegetables: ירקות (at least 3 a day)" in system
    assert "- snack: ביניים (at most 2 a day)" in system
    assert "- pizza: פיצה (at most 1 per week (Sunday to Saturday))" in system
    assert "- pre_workout: לפני אימון (extra: logged, but the bot keeps it out of the daily totals)" in system
    assert "# Counting rules for this plan" in system
    assert "כף הגשה גדושה" in system

    log_tool = prompts.router_tools(plan_menu)[0]
    item = log_tool["input_schema"]["properties"]["items"]["items"]
    assert item["properties"]["category"]["enum"] == plan_menu.category_keys
    assert "pre_workout" in log_tool["input_schema"]["properties"]["meal_type"]["enum"]


async def test_prompt_without_counting_rules_has_no_empty_section(menu):
    assert "Counting rules" not in prompts.router_system_prompt(menu)
