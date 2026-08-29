from subtitle_translator.config import Settings
from subtitle_translator.llm_client import QwenClient
from subtitle_translator.models import SubtitleDocument, SubtitleSegment, TranslationRequest


async def translate_document(
    request: TranslationRequest,
    settings: Settings,
    client: QwenClient | None = None,
) -> SubtitleDocument:
    llm = client or QwenClient(settings)
    translated_segments: list[SubtitleSegment] = []

    for offset in range(0, len(request.document.segments), settings.batch_size):
        batch = request.document.segments[offset : offset + settings.batch_size]
        translated = await llm.translate_batch(
            batch,
            source_language=request.document.source_language,
            target_language=request.target_language,
            glossary=request.glossary,
        )
        text_by_id = {item.id: item.text for item in translated}
        translated_segments.extend(
            SubtitleSegment(id=item.id, start=item.start, end=item.end, text=text_by_id[item.id])
            for item in batch
        )

    return SubtitleDocument(
        media_file=request.document.media_file,
        source_language=request.document.source_language,
        target_language=request.target_language,
        segments=translated_segments,
    )

