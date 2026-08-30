import asyncio

import pytest

from subtitle_translator.config import Settings
from subtitle_translator.glossary import GlossaryDocument, GlossaryTerm
from subtitle_translator.models import (
    SubtitleDocument,
    SubtitleSegment,
    TranslatedItem,
    TranslationRequest,
)
from subtitle_translator.service import GlossaryComplianceError, translate_document


class FakeClient:
    async def translate_batch(self, segments, source_language, target_language, glossary):
        return [TranslatedItem(id=item.id, text=f"中文：{item.text}") for item in segments]


def test_translation_preserves_timeline() -> None:
    request = TranslationRequest(
        document=SubtitleDocument(
            media_file="demo.mp4",
            source_language="en",
            segments=[SubtitleSegment(id=0, start=1, end=2, text="Hello")],
        )
    )
    result = asyncio.run(translate_document(request, Settings(), FakeClient()))
    assert result.target_language == "zh-CN"
    assert result.segments[0].start == 1
    assert result.segments[0].end == 2
    assert result.segments[0].text == "中文：Hello"


class RepairingClient:
    def __init__(self, repair_succeeds: bool = True) -> None:
        self.translate_calls = []
        self.repair_calls = []
        self.repair_succeeds = repair_succeeds

    async def translate_batch(self, segments, source_language, target_language, glossary):
        self.translate_calls.append((list(segments), list(glossary)))
        return [TranslatedItem(id=item.id, text="错误术语") for item in segments]

    async def repair_batch(self, segments, source_language, target_language, glossary):
        self.repair_calls.append((list(segments), list(glossary)))
        text = "这里使用旧化" if self.repair_succeeds else "仍然错误"
        return [TranslatedItem(id=item.id, text=text) for item in segments]


def glossary_request() -> TranslationRequest:
    return TranslationRequest(
        document=SubtitleDocument(
            media_file="demo.mp4",
            source_language="en",
            segments=[
                SubtitleSegment(id=0, start=0, end=1, text="Weathering effects"),
                SubtitleSegment(id=1, start=1, end=2, text="Ordinary sentence"),
            ],
        ),
        glossary=GlossaryDocument(
            terms=[
                GlossaryTerm(
                    source="weathering",
                    target="旧化",
                    aliases=["weathering effects"],
                ),
                GlossaryTerm(source="airbrush", target="喷笔"),
            ]
        ),
    )


def test_only_matched_terms_are_sent_and_failed_segment_is_repaired_once() -> None:
    client = RepairingClient()

    result = asyncio.run(
        translate_document(
            glossary_request(),
            Settings(_env_file=None, batch_size=20),
            client,
        )
    )

    assert result.segments[0].text == "这里使用旧化"
    assert result.segments[1].text == "错误术语"
    assert [term.source for term in client.translate_calls[0][1]] == ["weathering"]
    assert [segment.id for segment in client.repair_calls[0][0]] == [0]
    assert len(client.repair_calls) == 1


def test_glossary_failure_after_repair_rejects_translation() -> None:
    client = RepairingClient(repair_succeeds=False)

    with pytest.raises(GlossaryComplianceError, match="after one repair"):
        asyncio.run(
            translate_document(
                glossary_request(),
                Settings(_env_file=None, batch_size=20),
                client,
            )
        )

    assert len(client.repair_calls) == 1
