from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from subtitle_translator.config import Settings
from subtitle_translator.glossary import GlossaryDocument, GlossaryTerm, matched_terms
from subtitle_translator.llm_client import DeepSeekClient
from subtitle_translator.models import (
    SubtitleDocument,
    SubtitleSegment,
    TranslatedItem,
    TranslationRequest,
)


class GlossaryComplianceError(ValueError):
    pass


class TranslationClient(Protocol):
    async def translate_batch(
        self,
        segments: Sequence[SubtitleSegment],
        source_language: str,
        target_language: str,
        glossary: Sequence[GlossaryTerm],
    ) -> list[TranslatedItem]: ...


async def translate_document(
    request: TranslationRequest,
    settings: Settings,
    client: TranslationClient | None = None,
    progress: Callable[[], None] | None = None,
    initial_translations: Sequence[TranslatedItem] = (),
    batch_completed: Callable[[list[TranslatedItem]], None] | None = None,
) -> SubtitleDocument:
    llm = client or DeepSeekClient(settings)
    glossary = GlossaryDocument.from_value(request.glossary)
    source_segments = request.document.segments
    completed = list(initial_translations)
    _validate_resume_prefix(source_segments, completed, settings.batch_size)
    _validate_items_with_glossary(source_segments[: len(completed)], completed, glossary)

    for offset in range(len(completed), len(source_segments), settings.batch_size):
        batch = source_segments[offset : offset + settings.batch_size]
        batch_glossary = matched_terms([item.text for item in batch], glossary)
        translated = await llm.translate_batch(
            batch,
            source_language=request.document.source_language,
            target_language=request.target_language,
            glossary=batch_glossary,
        )
        _validate_item_ids(batch, translated)

        failed_indexes = _noncompliant_indexes(batch, translated, batch_glossary)
        if failed_indexes:
            failed_segments = [batch[index] for index in failed_indexes]
            repair_glossary = matched_terms([segment.text for segment in failed_segments], glossary)
            repair_method = getattr(llm, "repair_batch", llm.translate_batch)
            repaired = await repair_method(
                failed_segments,
                source_language=request.document.source_language,
                target_language=request.target_language,
                glossary=repair_glossary,
            )
            _validate_item_ids(failed_segments, repaired)
            for index, item in zip(failed_indexes, repaired):
                translated[index] = item
            remaining = _noncompliant_indexes(batch, translated, batch_glossary)
            if remaining:
                failed_ids = [batch[index].id for index in remaining]
                raise GlossaryComplianceError(
                    f"Glossary requirements still failed after one repair for segment IDs "
                    f"{failed_ids}"
                )

        completed.extend(translated)
        if progress is not None:
            progress()
        if batch_completed is not None:
            batch_completed(list(completed))

    translated_segments = [
        SubtitleSegment(
            id=source.id,
            start=source.start,
            end=source.end,
            text=translation.text,
        )
        for source, translation in zip(source_segments, completed)
    ]
    return SubtitleDocument(
        media_file=request.document.media_file,
        source_language=request.document.source_language,
        target_language=request.target_language,
        segments=translated_segments,
    )


def _validate_resume_prefix(
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem],
    batch_size: int,
) -> None:
    if len(translated) > len(source):
        raise ValueError("Saved translation progress has additional segments")
    if len(translated) != len(source) and len(translated) % batch_size:
        raise ValueError("Saved translation progress does not end at a batch boundary")
    _validate_item_ids(source[: len(translated)], translated)


def _validate_item_ids(
    source: Sequence[SubtitleSegment], translated: Sequence[TranslatedItem]
) -> None:
    expected_ids = [item.id for item in source]
    actual_ids = [item.id for item in translated]
    if actual_ids != expected_ids:
        raise ValueError(
            f"Translation changed batch IDs: expected {expected_ids}, got {actual_ids}"
        )


def _validate_items_with_glossary(
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem],
    glossary: GlossaryDocument,
) -> None:
    terms = matched_terms([item.text for item in source], glossary)
    failed = _noncompliant_indexes(source, translated, terms)
    if failed:
        raise ValueError("Saved translation progress does not satisfy the current glossary")


def _noncompliant_indexes(
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem],
    terms: Sequence[GlossaryTerm],
) -> list[int]:
    failed: list[int] = []
    for index, (source_item, translated_item) in enumerate(zip(source, translated)):
        required = matched_terms([source_item.text], GlossaryDocument(terms=list(terms)))
        if any(term.target not in translated_item.text for term in required):
            failed.append(index)
    return failed
