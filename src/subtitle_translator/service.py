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
    # Revalidate saved prefixes and complete cached documents under the current rules.
    for offset in range(0, len(completed), settings.batch_size):
        source = source_segments[offset : offset + settings.batch_size]
        saved = completed[offset : offset + settings.batch_size]
        repaired = await repair_noncompliant(source, saved, glossary, request, llm)
        completed[offset : offset + len(saved)] = repaired
        if progress is not None:
            progress()
        if repaired != saved and batch_completed is not None:
            batch_completed(list(completed))

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

        translated = await repair_noncompliant(batch, translated, glossary, request, llm)

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
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem] | Sequence[SubtitleSegment],
) -> None:
    expected_ids = [item.id for item in source]
    actual_ids = [item.id for item in translated]
    if actual_ids != expected_ids:
        raise ValueError(
            f"Translation changed batch IDs: expected {expected_ids}, got {actual_ids}"
        )


def missing_required_terms(
    source_text: str, translated_text: str, glossary: GlossaryDocument
) -> list[GlossaryTerm]:
    return [
        term
        for term in matched_terms([source_text], glossary)
        if term.enforcement == "required" and not term.accepts(translated_text)
    ]


def noncompliant_indexes(
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem] | Sequence[SubtitleSegment],
    glossary: GlossaryDocument,
) -> list[int]:
    _validate_item_ids(source, translated)
    return [
        index
        for index, (original, result) in enumerate(zip(source, translated))
        if missing_required_terms(original.text, result.text, glossary)
    ]


async def repair_noncompliant(
    source: Sequence[SubtitleSegment],
    translated: Sequence[TranslatedItem],
    glossary: GlossaryDocument,
    request: TranslationRequest,
    llm: TranslationClient,
) -> list[TranslatedItem]:
    result = list(translated)
    failed_indexes = noncompliant_indexes(source, result, glossary)
    if not failed_indexes:
        return result
    failed_segments = [source[index] for index in failed_indexes]
    previous = [result[index] for index in failed_indexes]
    # Keep preferred terms too: repair must use the same contextual guidance as translation.
    repair_glossary = matched_terms([segment.text for segment in failed_segments], glossary)
    repair_method = getattr(llm, "repair_batch", None)
    kwargs = {
        "source_language": request.document.source_language,
        "target_language": request.target_language,
        "glossary": repair_glossary,
    }
    if repair_method is None:
        repaired = await llm.translate_batch(failed_segments, **kwargs)
    else:
        reasons = {
            original.id: [
                f"缺少必需术语 {term.source!r} 的译文；允许："
                + "、".join((term.target, *term.accepted_targets))
                for term in missing_required_terms(original.text, item.text, glossary)
            ]
            for original, item in zip(failed_segments, previous)
        }
        repaired = await repair_method(
            failed_segments, previous_translations=previous, failure_reasons=reasons, **kwargs
        )
    _validate_item_ids(failed_segments, repaired)
    for index, item in zip(failed_indexes, repaired):
        result[index] = item
    remaining = noncompliant_indexes(source, result, glossary)
    if remaining:
        failed_ids = [source[index].id for index in remaining]
        raise GlossaryComplianceError(
            f"Glossary requirements still failed after one repair for segment IDs {failed_ids}"
        )
    return result
