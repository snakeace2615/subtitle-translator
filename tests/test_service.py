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

    async def repair_batch(
        self,
        segments,
        source_language,
        target_language,
        glossary,
        *,
        previous_translations=(),
        failure_reasons=None,
    ):
        self.previous_translations = previous_translations
        self.failure_reasons = failure_reasons
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
                GlossaryTerm(
                    source="ordinary",
                    target="普通",
                    enforcement="preferred",
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
    assert [term.source for term in client.translate_calls[0][1]] == [
        "weathering",
        "ordinary",
    ]
    assert [segment.id for segment in client.repair_calls[0][0]] == [0]
    assert [term.source for term in client.repair_calls[0][1]] == ["weathering"]
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


@pytest.mark.parametrize("resume", [False, True])
def test_accepted_target_avoids_unnecessary_repair(resume) -> None:
    request = glossary_request()
    request.glossary.terms[0].accepted_targets = ["做旧"]
    items = [TranslatedItem(id=0, text="这里做旧"), TranslatedItem(id=1, text="普通句子")]

    class Client(RepairingClient):
        async def translate_batch(self, *args, **kwargs):
            return items

    client = Client()
    result = asyncio.run(
        translate_document(
            request,
            Settings(_env_file=None),
            client,
            initial_translations=items if resume else (),
        )
    )
    assert [item.text for item in result.segments] == [item.text for item in items]
    assert client.repair_calls == []


def test_saved_translations_are_repaired_with_feedback_without_retranslating() -> None:
    request = glossary_request()
    saved = [TranslatedItem(id=0, text="这里做旧"), TranslatedItem(id=1, text="保留这条")]
    snapshots = []
    client = RepairingClient()
    result = asyncio.run(
        translate_document(
            request,
            Settings(_env_file=None, batch_size=1),
            client,
            initial_translations=saved,
            batch_completed=snapshots.append,
        )
    )
    assert not client.translate_calls
    assert [[item.id for item in call[0]] for call in client.repair_calls] == [[0]]
    assert client.previous_translations == [saved[0]]
    assert "旧化" in client.failure_reasons[0][0]
    assert result.segments[1].text == "保留这条"
    assert snapshots[-1][0].text == "这里使用旧化"
    assert saved[0].text == "这里做旧"


def test_repository_ambiguous_terms_allow_contextual_translation() -> None:
    from pathlib import Path

    from subtitle_translator.glossary import load_glossary
    from subtitle_translator.service import noncompliant_indexes

    glossary = load_glossary(Path(__file__).parents[1] / "config/glossary.json")
    source = [SubtitleSegment(id=0, start=0, end=1, text="Wash your brush with water.")]
    result = [TranslatedItem(id=0, text="用水清洗你的刷子。")]
    assert noncompliant_indexes(source, result, glossary) == []


def test_long_preferred_phrase_does_not_require_overlapping_short_term() -> None:
    from subtitle_translator.service import noncompliant_indexes

    glossary = GlossaryDocument(
        terms=[
            GlossaryTerm(source="base", target="地台"),
            GlossaryTerm(source="base coat", target="基础色", enforcement="preferred"),
        ]
    )
    source = [SubtitleSegment(id=0, start=0, end=1, text="Apply a base-coat.")]
    assert noncompliant_indexes(source, [TranslatedItem(id=0, text="涂一层底色。")], glossary) == []
