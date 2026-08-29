import asyncio

from subtitle_translator.config import Settings
from subtitle_translator.models import (
    SubtitleDocument,
    SubtitleSegment,
    TranslatedItem,
    TranslationRequest,
)
from subtitle_translator.service import translate_document


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

