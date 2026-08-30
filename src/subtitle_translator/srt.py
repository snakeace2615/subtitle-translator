from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from subtitle_translator.models import SubtitleDocument

TIMESTAMP_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


def render_srt(document: SubtitleDocument) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(document.segments, start=1):
        text_lines = [line.strip() for line in segment.text.splitlines() if line.strip()]
        if not text_lines:
            raise ValueError(f"Subtitle segment {segment.id} has empty SRT text")
        text = "\n".join(text_lines)
        blocks.append(
            f"{index}\n{format_timestamp(segment.start)} --> "
            f"{format_timestamp(segment.end)}\n{text}"
        )
    value = "\n\n".join(blocks) + ("\n" if blocks else "")
    validate_srt(value, len(document.segments))
    return value


def validate_srt(value: str, expected_segments: int) -> None:
    if expected_segments == 0:
        if value:
            raise ValueError("Empty subtitle document produced non-empty SRT")
        return
    blocks = value.rstrip("\n").split("\n\n")
    if len(blocks) != expected_segments:
        raise ValueError(
            f"SRT block count changed: expected {expected_segments}, got {len(blocks)}"
        )
    for index, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0] != str(index) or not TIMESTAMP_PATTERN.fullmatch(lines[1]):
            raise ValueError(f"Invalid SRT block {index}")


def publish_srt(path: Path, value: str, expected_segments: int) -> str:
    validate_srt(value, expected_segments)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        validate_srt(temporary_path.read_text(encoding="utf-8"), expected_segments)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return sha256_bytes(value.encode("utf-8"))


def format_timestamp(seconds: float) -> str:
    total_milliseconds = round(seconds * 1000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
