from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, ConfigDict

from subtitle_translator.config import Settings
from subtitle_translator.glossary import GlossaryDocument, GlossaryTerm, matched_terms
from subtitle_translator.models import SubtitleSegment, TranslatedItem

LOGGER = logging.getLogger(__name__)

DEEPSEEK_MODEL = "deepseek-v4-flash"
SYSTEM_PROMPT = """你是专业字幕翻译器。将输入字幕翻译为用户指定的目标语言。
要求：
1. 保留语气、专有名词和上下文，不要解释。
2. 译文应简洁、自然，适合屏幕阅读。
3. 每个字幕的 glossary 只适用于该字幕，usage 说明术语适用词义。
   required：使用 target 或 accepted_targets 中的译文，优先 target。
   preferred：仅在上下文符合该专业词义时优先采用 target；含义不同时按实际含义翻译。
   不得强行套用多义词，不得添加原文没有的含义，不得丢失否定关系或更改数字。
4. 必须只返回一个 JSON 对象，格式为
   {"translations":[{"id":1,"text":"译文"}]}。
5. translations 中的 id 数量、取值和顺序必须与输入字幕完全一致。
6. 不要返回 Markdown 代码块或 JSON 对象以外的内容。
"""
REPAIR_PROMPT = """上次译文为空或没有遵守指定术语。请只修复输入的字幕片段。
参考 previous_translation 和 failure_reasons 修复具体问题，保留原文含义。
每个 text 都必须包含非空译文。仍须遵守术语等级：required 接受 target 或 accepted_targets，
preferred 按上下文选词，不得强行套用。只返回以下格式的 JSON 对象：
{"translations":[{"id":1,"text":"修复后的译文"}]}
id 数量、取值和顺序必须与输入完全一致，不要解释。
"""
PROMPT_VERSION = 4
OUTPUT_CONTRACT_VERSION = "translated-items-object/v2"
CONTEXT_STRATEGY = "independent-batches-with-resume/v2"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})


class DeepSeekConfigurationError(ValueError):
    pass


class DeepSeekAPIError(RuntimeError):
    pass


class RawTranslatedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str | None = None


class RawTranslationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    translations: list[RawTranslatedItem]


def validate_deepseek_settings(settings: Settings) -> None:
    key = settings.llm_api_key.strip()
    placeholders = {"", "local", "changeme", "your-api-key", "<deepseek api key>"}
    if key.casefold() in placeholders:
        raise DeepSeekConfigurationError("DeepSeek API key is missing or still a placeholder")
    if settings.llm_provider.strip().casefold() != "deepseek":
        raise DeepSeekConfigurationError("LLM provider must be 'deepseek'")
    if settings.llm_model != DEEPSEEK_MODEL:
        raise DeepSeekConfigurationError(f"LLM model must be '{DEEPSEEK_MODEL}'")
    if settings.llm_thinking:
        raise DeepSeekConfigurationError("LLM thinking mode must be disabled for translation")


class DeepSeekClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        validate_deepseek_settings(settings)
        self.settings = settings
        self.transport = transport
        self.sleep = sleep

    async def translate_batch(
        self,
        segments: Sequence[SubtitleSegment],
        source_language: str,
        target_language: str,
        glossary: Sequence[GlossaryTerm],
    ) -> list[TranslatedItem]:
        return await self._translate(
            segments, source_language, target_language, glossary, repair=False
        )

    async def repair_batch(
        self,
        segments: Sequence[SubtitleSegment],
        source_language: str,
        target_language: str,
        glossary: Sequence[GlossaryTerm],
        *,
        previous_translations: Sequence[TranslatedItem] = (),
        failure_reasons: dict[int, list[str]] | None = None,
    ) -> list[TranslatedItem]:
        return await self._translate(
            segments,
            source_language,
            target_language,
            glossary,
            repair=True,
            previous_texts={item.id: item.text for item in previous_translations},
            failure_reasons=failure_reasons,
        )

    async def _translate(
        self,
        segments: Sequence[SubtitleSegment],
        source_language: str,
        target_language: str,
        glossary: Sequence[GlossaryTerm],
        *,
        repair: bool,
        previous_texts: dict[int, str | None] | None = None,
        failure_reasons: dict[int, list[str]] | None = None,
    ) -> list[TranslatedItem]:
        document = GlossaryDocument(terms=list(glossary))
        subtitles: list[dict[str, object]] = []
        for item in segments:
            subtitle: dict[str, object] = {
                "id": item.id,
                "text": item.text,
                "glossary": [
                    term.model_dump(mode="json") for term in matched_terms([item.text], document)
                ],
            }
            if repair:
                subtitle["previous_translation"] = (previous_texts or {}).get(item.id)
                subtitle["failure_reasons"] = (failure_reasons or {}).get(
                    item.id, ["译文缺失或为空"]
                )
            subtitles.append(subtitle)
        payload = {
            "source_language": source_language,
            "target_language": target_language,
            "subtitles": subtitles,
        }
        body = {
            "model": DEEPSEEK_MODEL,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.llm_max_output_tokens,
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *([{"role": "system", "content": REPAIR_PROMPT}] if repair else []),
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        response, elapsed = await self._post(body)
        translated = self._parse_response(response, segments)
        usage = response.json().get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        LOGGER.info(
            "DeepSeek batch completed segments=%d repair=%s elapsed_seconds=%.3f "
            "prompt_tokens=%s completion_tokens=%s total_tokens=%s",
            len(segments),
            repair,
            elapsed,
            usage.get("prompt_tokens", "unknown"),
            usage.get("completion_tokens", "unknown"),
            usage.get("total_tokens", "unknown"),
        )
        incomplete_indexes = [
            index
            for index, item in enumerate(translated)
            if item is None or not item.text or not item.text.strip()
        ]
        if incomplete_indexes:
            if repair:
                incomplete_ids = [segments[index].id for index in incomplete_indexes]
                raise DeepSeekAPIError(
                    "DeepSeek returned missing or empty translations after one repair for IDs "
                    f"{incomplete_ids}"
                )
            failed_segments = [segments[index] for index in incomplete_indexes]
            repair_glossary = matched_terms(
                [segment.text for segment in failed_segments],
                GlossaryDocument(terms=list(glossary)),
            )
            repaired = await self._translate(
                failed_segments,
                source_language,
                target_language,
                repair_glossary,
                repair=True,
                previous_texts={
                    segments[index].id: translated[index].text if translated[index] else None
                    for index in incomplete_indexes
                },
            )
            for index, item in zip(incomplete_indexes, repaired):
                translated[index] = RawTranslatedItem(id=item.id, text=item.text)
        result: list[TranslatedItem] = []
        for item in translated:
            if item is None:
                raise AssertionError("targeted translation repair left a missing item")
            result.append(TranslatedItem.model_validate(item.model_dump()))
        return result

    async def _post(self, body: dict[str, object]) -> tuple[httpx.Response, float]:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key.strip()}"}
        started = time.monotonic()
        async with httpx.AsyncClient(
            timeout=self.settings.timeout_seconds, transport=self.transport
        ) as client:
            for attempt in range(self.settings.llm_retry_attempts):
                try:
                    response = await client.post(url, headers=headers, json=body)
                except httpx.TransportError as exc:
                    if attempt + 1 >= self.settings.llm_retry_attempts:
                        raise DeepSeekAPIError(
                            f"DeepSeek API request failed after {attempt + 1} attempts: "
                            f"{type(exc).__name__}"
                        ) from exc
                    await self.sleep(self._backoff_seconds(attempt, None))
                    continue

                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt + 1 >= self.settings.llm_retry_attempts:
                        raise DeepSeekAPIError(
                            f"DeepSeek API returned HTTP {response.status_code} after "
                            f"{attempt + 1} attempts"
                        )
                    await self.sleep(self._backoff_seconds(attempt, response))
                    continue
                if response.is_error:
                    raise DeepSeekAPIError(
                        f"DeepSeek API returned non-retryable HTTP {response.status_code}"
                    )
                return response, time.monotonic() - started
        raise AssertionError("unreachable DeepSeek retry loop")

    def _backoff_seconds(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                return retry_after
        return self.settings.llm_retry_backoff_seconds * (2**attempt)

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        segments: Sequence[SubtitleSegment],
    ) -> list[RawTranslatedItem | None]:
        try:
            value = response.json()
            choices = value["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("response must contain exactly one choice")
            choice = choices[0]
            finish_reason = choice["finish_reason"]
            if finish_reason != "stop":
                raise ValueError(f"generation did not finish normally: {finish_reason!r}")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("response content is empty")
            envelope = RawTranslationEnvelope.model_validate_json(content)
        except (KeyError, TypeError, ValueError) as exc:
            raise DeepSeekAPIError(f"Invalid DeepSeek response: {exc}") from exc

        expected_ids = [item.id for item in segments]
        actual_ids = [item.id for item in envelope.translations]
        if len(actual_ids) != len(set(actual_ids)):
            raise DeepSeekAPIError(f"DeepSeek returned duplicate subtitle ids: {actual_ids}")
        expected_id_set = set(expected_ids)
        unexpected_ids = [item_id for item_id in actual_ids if item_id not in expected_id_set]
        if unexpected_ids:
            raise DeepSeekAPIError(f"DeepSeek returned unexpected subtitle ids: {unexpected_ids}")
        actual_id_set = set(actual_ids)
        expected_present_order = [item_id for item_id in expected_ids if item_id in actual_id_set]
        if actual_ids != expected_present_order:
            raise DeepSeekAPIError(
                f"DeepSeek reordered subtitle ids: expected {expected_present_order}, got {actual_ids}"
            )
        by_id = {item.id: item for item in envelope.translations}
        return [by_id.get(item_id) for item_id in expected_ids]


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
