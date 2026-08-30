import pytest
from pydantic import ValidationError

from subtitle_translator.models import SubtitleDocument


def document_with_segments(segments) -> dict:
    return {
        "schema_version": "subtitle-document/v1",
        "media_file": "demo.mp4",
        "source_language": "en",
        "segments": segments,
    }


def test_rejects_unknown_schema_version() -> None:
    value = document_with_segments([])
    value["schema_version"] = "subtitle-document/v2"

    with pytest.raises(ValidationError):
        SubtitleDocument.model_validate(value)


def test_rejects_duplicate_ids_and_unordered_segments() -> None:
    duplicate = document_with_segments(
        [
            {"id": 0, "start": 0, "end": 1, "text": "one"},
            {"id": 0, "start": 1, "end": 2, "text": "two"},
        ]
    )
    unordered = document_with_segments(
        [
            {"id": 0, "start": 2, "end": 3, "text": "one"},
            {"id": 1, "start": 1, "end": 2, "text": "two"},
        ]
    )

    with pytest.raises(ValidationError, match="duplicate segment id"):
        SubtitleDocument.model_validate(duplicate)
    with pytest.raises(ValidationError, match="ordered by start time"):
        SubtitleDocument.model_validate(unordered)


def test_rejects_blank_subtitle_text() -> None:
    value = document_with_segments([{"id": 0, "start": 0, "end": 1, "text": "   "}])

    with pytest.raises(ValidationError, match="text must not be blank"):
        SubtitleDocument.model_validate(value)
