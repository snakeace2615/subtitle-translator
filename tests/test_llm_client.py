import asyncio
import json

import httpx
import pytest

from subtitle_translator.config import Settings
from subtitle_translator.glossary import GlossaryTerm
from subtitle_translator.llm_client import (
    DEEPSEEK_MODEL,
    DeepSeekAPIError,
    DeepSeekClient,
    DeepSeekConfigurationError,
)
from subtitle_translator.models import SubtitleSegment


def make_settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "test-secret-key",
        "llm_base_url": "https://api.deepseek.com",
        "llm_model": DEEPSEEK_MODEL,
        "llm_retry_backoff_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def response_payload(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }


def test_deepseek_request_uses_official_contract() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=response_payload('{"translations":[{"id":7,"text":"你好"}]}'),
        )

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    translated = asyncio.run(
        client.translate_batch(
            [SubtitleSegment(id=7, start=0, end=1, text="Hello")],
            "en",
            "zh-CN",
            [GlossaryTerm(source="hello", target="你好", enforcement="preferred")],
        )
    )

    assert translated[0].text == "你好"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer test-secret-key"
    body = captured["body"]
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 4096
    assert "JSON" in body["messages"][0]["content"]
    prompt = json.loads(body["messages"][-1]["content"])
    assert prompt["subtitles"][0]["glossary"] == [
        {
            "source": "hello",
            "target": "你好",
            "aliases": [],
            "case_sensitive": False,
            "enforcement": "preferred",
            "accepted_targets": [],
            "usage": None,
        }
    ]


def test_retryable_statuses_honor_retry_after_and_eventually_succeed() -> None:
    statuses = [429, 500, 503, 200]
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(
                200,
                json=response_payload('{"translations":[{"id":0,"text":"好"}]}'),
            )
        headers = {"Retry-After": "2"} if status == 429 else {}
        return httpx.Response(status, headers=headers)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = DeepSeekClient(
        make_settings(llm_retry_attempts=4),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    asyncio.run(
        client.translate_batch(
            [SubtitleSegment(id=0, start=0, end=1, text="OK")], "en", "zh-CN", []
        )
    )

    assert sleeps == [2.0, 0.0, 0.0]
    assert statuses == []


def test_transport_error_is_retried_without_leaking_key() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json=response_payload('{"translations":[{"id":0,"text":"好"}]}'),
        )

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    result = asyncio.run(
        client.translate_batch(
            [SubtitleSegment(id=0, start=0, end=1, text="OK")], "en", "zh-CN", []
        )
    )

    assert calls == 2
    assert result[0].text == "好"


def test_empty_translation_is_repaired_for_only_the_failed_segment() -> None:
    responses = [
        response_payload('{"translations":[{"id":7,"text":"保留这条"},{"id":8,"text":""}]}'),
        response_payload('{"translations":[{"id":8,"text":"几分钟。"}]}'),
    ]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    translated = asyncio.run(
        client.translate_batch(
            [
                SubtitleSegment(id=7, start=0, end=1, text="Keep this"),
                SubtitleSegment(id=8, start=1, end=2, text="a few minutes."),
            ],
            "en",
            "zh-CN",
            [GlossaryTerm(source="minutes", target="分钟")],
        )
    )

    assert [(item.id, item.text) for item in translated] == [
        (7, "保留这条"),
        (8, "几分钟。"),
    ]
    assert len(requests) == 2
    repaired_payload = json.loads(requests[1]["messages"][-1]["content"])
    [subtitle] = repaired_payload["subtitles"]
    assert subtitle["id"] == 8
    assert subtitle["text"] == "a few minutes."
    assert subtitle["glossary"][0]["target"] == "分钟"
    assert subtitle["previous_translation"] == ""
    assert subtitle["failure_reasons"] == ["译文缺失或为空"]
    assert "为空" in requests[1]["messages"][1]["content"]


def test_empty_translation_still_fails_after_one_targeted_repair() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=response_payload('{"translations":[{"id":0,"text":" "}]}'),
        )

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekAPIError, match=r"after one repair for IDs \[0\]"):
        asyncio.run(
            client.translate_batch(
                [SubtitleSegment(id=0, start=0, end=1, text="Hello")],
                "en",
                "zh-CN",
                [],
            )
        )

    assert calls == 2


def test_missing_translation_id_is_repaired_without_repeating_valid_items() -> None:
    responses = [
        response_payload('{"translations":[{"id":40,"text":"四十"},{"id":42,"text":"四十二"}]}'),
        response_payload('{"translations":[{"id":41,"text":"四十一"}]}'),
    ]
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    translated = asyncio.run(
        client.translate_batch(
            [
                SubtitleSegment(id=40, start=0, end=1, text="forty"),
                SubtitleSegment(id=41, start=1, end=2, text="forty-one"),
                SubtitleSegment(id=42, start=2, end=3, text="forty-two"),
            ],
            "en",
            "zh-CN",
            [],
        )
    )

    assert [(item.id, item.text) for item in translated] == [
        (40, "四十"),
        (41, "四十一"),
        (42, "四十二"),
    ]
    repaired_payload = json.loads(requests[1]["messages"][-1]["content"])
    [subtitle] = repaired_payload["subtitles"]
    assert subtitle == {
        "id": 41,
        "text": "forty-one",
        "glossary": [],
        "previous_translation": None,
        "failure_reasons": ["译文缺失或为空"],
    }


@pytest.mark.parametrize(
    "content",
    [
        ('{"translations":[{"id":0,"text":"零"},{"id":0,"text":"重复"}]}'),
        ('{"translations":[{"id":1,"text":"一"},{"id":0,"text":"零"}]}'),
        ('{"translations":[{"id":0,"text":"零"},{"id":99,"text":"额外"}]}'),
    ],
)
def test_duplicate_reordered_or_unexpected_ids_are_not_repaired(content: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload(content))

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekAPIError):
        asyncio.run(
            client.translate_batch(
                [
                    SubtitleSegment(id=0, start=0, end=1, text="zero"),
                    SubtitleSegment(id=1, start=1, end=2, text="one"),
                ],
                "en",
                "zh-CN",
                [],
            )
        )

    assert calls == 1


@pytest.mark.parametrize("status", [400, 401, 402, 422])
def test_non_retryable_status_fails_once_without_exposing_key(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="server detail")

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekAPIError) as caught:
        asyncio.run(
            client.translate_batch(
                [SubtitleSegment(id=0, start=0, end=1, text="Hello")],
                "en",
                "zh-CN",
                [],
            )
        )

    assert calls == 1
    assert "test-secret-key" not in str(caught.value)
    assert "server detail" not in str(caught.value)


@pytest.mark.parametrize(
    ("content", "finish_reason", "error"),
    [
        ("", "stop", "empty"),
        ('{"translations":[]}', "length", "finish"),
        ('[{"id":0,"text":"bad"}]', "stop", "Invalid"),
        ('{"translations":[{"id":1,"text":"bad"}]}', "stop", "ids"),
    ],
)
def test_invalid_or_truncated_responses_are_rejected(
    content: str, finish_reason: str, error: str
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json=response_payload(content, finish_reason=finish_reason)
        )
    )
    client = DeepSeekClient(make_settings(), transport=transport)
    with pytest.raises(DeepSeekAPIError, match=error):
        asyncio.run(
            client.translate_batch(
                [SubtitleSegment(id=0, start=0, end=1, text="Hello")],
                "en",
                "zh-CN",
                [],
            )
        )


@pytest.mark.parametrize("key", ["", "local", "changeme", "your-api-key"])
def test_missing_or_placeholder_api_key_is_rejected(key: str) -> None:
    with pytest.raises(DeepSeekConfigurationError, match="missing or still a placeholder"):
        DeepSeekClient(make_settings(llm_api_key=key))


def test_repair_payload_scopes_glossary_and_includes_feedback() -> None:
    from subtitle_translator.models import TranslatedItem

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=response_payload(
                '{"translations":[{"id":1,"text":"清洗刷子"},{"id":2,"text":"给负重轮上色"}]}'
            ),
        )

    client = DeepSeekClient(make_settings(), transport=httpx.MockTransport(handler))
    asyncio.run(
        client.repair_batch(
            [
                SubtitleSegment(id=1, start=0, end=1, text="Wash your brush."),
                SubtitleSegment(id=2, start=1, end=2, text="Paint road wheels."),
            ],
            "en",
            "zh-CN",
            [
                GlossaryTerm(
                    source="wash", target="渍洗", enforcement="preferred", usage="旧化技法"
                ),
                GlossaryTerm(
                    source="road wheel",
                    target="负重轮",
                    aliases=["road wheels"],
                    accepted_targets=["承重轮"],
                ),
            ],
            previous_translations=[TranslatedItem(id=2, text="给轮子上色")],
            failure_reasons={2: ["缺少负重轮"]},
        )
    )
    payload = json.loads(requests[0]["messages"][-1]["content"])
    first, second = payload["subtitles"]
    assert [term["source"] for term in first["glossary"]] == ["wash"]
    assert first["glossary"][0]["enforcement"] == "preferred"
    assert first["glossary"][0]["usage"] == "旧化技法"
    assert [term["source"] for term in second["glossary"]] == ["road wheel"]
    assert second["glossary"][0]["accepted_targets"] == ["承重轮"]
    assert second["previous_translation"] == "给轮子上色"
    assert second["failure_reasons"] == ["缺少负重轮"]
    assert all("preferred" in message["content"] for message in requests[0]["messages"][:2])


def test_legacy_model_is_rejected_with_current_model_name() -> None:
    with pytest.raises(DeepSeekConfigurationError, match="deepseek-flash"):
        DeepSeekClient(make_settings(llm_model="deepseek-v4-flash"))
