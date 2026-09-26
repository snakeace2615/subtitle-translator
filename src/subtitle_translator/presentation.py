"""Display-only layout and diagnostic checks; never modify translation metadata."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field

from subtitle_translator.models import SubtitleDocument, SubtitleSegment


class LayoutOptions(BaseModel):
    protected_terms: tuple[str, ...] = ()
    line_width: int = Field(default=24, ge=8, le=80)
    max_lines: int = Field(default=2, ge=1, le=4)
    max_duration: float = Field(default=8.0, gt=0)
    max_reading_speed: float = Field(default=12.0, gt=0)

    @property
    def fingerprint(self) -> str:
        payload = {"version": 1, **self.model_dump()}
        content = json.dumps(payload, sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(content).hexdigest()


class QualityIssue(BaseModel):
    code: str
    segment_ids: list[int]
    start_seconds: float
    end_seconds: float
    severity: Literal["info", "warning"] = "warning"
    message: str


class QualityReport(BaseModel):
    version: Literal[1] = 1
    layout_fingerprint: str
    source_segments: int
    output_cues: int
    source_sha256: str | None = None
    translation_sha256: str | None = None
    issues: list[QualityIssue]


class Presentation(BaseModel):
    cues: list[SubtitleSegment]
    report: QualityReport


def display_width(text: str) -> float:
    return sum(
        0
        if unicodedata.combining(char)
        else 1
        if unicodedata.east_asian_width(char) in "WF"
        else 0.5
        for char in text
    )


def wrap_text(text: str, width: int, protected_terms: tuple[str, ...] = ()) -> list[str]:
    lines: list[str] = []
    # Preserve explicit line breaks. Prefer punctuation/word boundaries to arbitrary splits.
    for paragraph in text.splitlines():
        remaining = paragraph.strip()
        while remaining:
            if display_width(remaining) <= width:
                lines.append(remaining)
                break
            used = 0.0
            limit = 0
            for index, char in enumerate(remaining):
                used += display_width(char)
                if used > width:
                    break
                limit = index + 1
            if limit == 0:
                raise ValueError("Subtitle character exceeds configured line width")
            boundary = 0
            for index in range(1, limit + 1):
                left = remaining[index - 1]
                right = remaining[index : index + 1]
                decimal_point = (
                    left == "." and index > 1 and remaining[index - 2].isdigit() and right.isdigit()
                )
                if (
                    (left.isspace() or left in "，。！？；：、,.!?;:")
                    and display_width(remaining[:index]) >= width * 0.45
                    and not decimal_point
                ):
                    boundary = index
            if not boundary:
                # Keep ordinary Latin words and numbers together where possible.
                boundary = limit
                while (
                    boundary > 0
                    and remaining[boundary - 1].isascii()
                    and remaining[boundary - 1].isalnum()
                    and remaining[boundary : boundary + 1].isascii()
                    and remaining[boundary : boundary + 1].isalnum()
                ):
                    boundary -= 1
                if boundary == 0 or display_width(remaining[:boundary]) < width * 0.45:
                    boundary = limit
                while (
                    boundary > 1
                    and remaining[boundary : boundary + 1] in "，。！？；：、,.!?;:)]）】"
                ):
                    boundary -= 1
            # Do not split known glossary translations that fit on a line.
            protected_spans: list[tuple[int, int]] = []
            for term in sorted(set(protected_terms), key=len, reverse=True):
                if not term or display_width(term) > width:
                    continue
                for match in re.finditer(re.escape(term), remaining):
                    span = match.span()
                    if not any(span[0] < b and a < span[1] for a, b in protected_spans):
                        protected_spans.append(span)
            for start, end in protected_spans:
                if start < boundary < end:
                    boundary = end if end <= limit else start
                    break
            lines.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
    return lines


def build_presentation(
    document: SubtitleDocument,
    options: LayoutOptions | None = None,
    source: SubtitleDocument | None = None,
) -> Presentation:
    options = options or LayoutOptions()
    cues: list[SubtitleSegment] = []
    issues: list[QualityIssue] = []
    sources = {item.id: item for item in source.segments} if source else {}
    repeat_run: list[SubtitleSegment] = []
    by_id = {item.id: item for item in document.segments}

    def flag(code: str, ids: list[int], message: str, severity: str = "warning") -> None:
        issues.append(
            QualityIssue(
                code=code,
                segment_ids=ids,
                message=message,
                severity=severity,
                start_seconds=min(by_id[i].start for i in ids),
                end_seconds=max(by_id[i].end for i in ids),
            )
        )

    def finish_repeats() -> None:
        if len(repeat_run) >= 3:
            flag(
                "repeated_cues",
                [item.id for item in repeat_run],
                "连续至少三条相同字幕；可能为重复识别，请核对音频，未自动删除。",
            )

    for index, segment in enumerate(document.segments):
        duration = segment.end - segment.start
        if duration > options.max_duration:
            flag("long_duration", [segment.id], f"持续 {duration:.2f} 秒，需核对语音结束时间。")
        speed = display_width(re.sub(r"\s+", "", segment.text)) / duration
        if speed > options.max_reading_speed:
            flag(
                "reading_speed",
                [segment.id],
                f"阅读速度约 {speed:.1f} 汉字宽度/秒，需核对翻译及断句。",
            )
        if index and segment.start < document.segments[index - 1].end:
            flag("overlap", [document.segments[index - 1].id, segment.id], "相邻源片段时间重叠。")
        normalized = re.sub(r"\W+", "", segment.text).casefold()
        if repeat_run and (
            re.sub(r"\W+", "", repeat_run[-1].text).casefold() != normalized
            or segment.start - repeat_run[-1].end > 1.0
        ):
            finish_repeats()
            repeat_run = []
        if normalized:
            repeat_run.append(segment)
        tokens = re.findall(r"[A-Za-z]+|[\u3400-\u9fff]", segment.text.casefold())
        if len(tokens) >= 10 and max(Counter(tokens).values(), default=0) / len(tokens) >= 0.65:
            flag("repetitive_text", [segment.id], "单条字幕高度重复，可能存在识别或翻译异常。")
        original = sources.get(segment.id)
        if original and display_width(segment.text) > max(60, display_width(original.text) * 3):
            flag(
                "translation_expansion",
                [segment.id],
                "译文明显长于原文，需核对是否错配或添加内容。",
            )
        lines = wrap_text(segment.text, options.line_width, options.protected_terms)
        if not lines:
            raise ValueError(f"Subtitle segment {segment.id} has empty SRT text")
        pages = [lines[i : i + options.max_lines] for i in range(0, len(lines), options.max_lines)]
        if len(pages) > 1:
            flag(
                "estimated_page_timing",
                [segment.id],
                "按文字宽度分配多屏时间；未进行中文与音频对齐，需检查同步。",
                "info",
            )
        start_ms, end_ms = round(segment.start * 1000), round(segment.end * 1000)
        if end_ms - start_ms < len(pages):
            raise ValueError(f"Subtitle segment {segment.id} is too short at millisecond precision")
        weights = [display_width("".join(page)) for page in pages]
        total = sum(weights)
        elapsed = 0.0
        cursor = start_ms
        for page_index, (page, weight) in enumerate(zip(pages, weights)):
            elapsed += weight
            boundary = (
                end_ms
                if page_index == len(pages) - 1
                else min(
                    end_ms - (len(pages) - page_index - 1),
                    max(cursor + 1, start_ms + round((end_ms - start_ms) * elapsed / total)),
                )
            )
            cues.append(
                SubtitleSegment(
                    id=len(cues) + 1, start=cursor / 1000, end=boundary / 1000, text="\n".join(page)
                )
            )
            cursor = boundary
    finish_repeats()
    # Overlapping source segments can interleave display pages; retain every cue in time order.
    cues.sort(key=lambda item: (item.start, item.id))
    return Presentation(
        cues=cues,
        report=QualityReport(
            layout_fingerprint=options.fingerprint,
            source_segments=len(document.segments),
            output_cues=len(cues),
            issues=issues,
        ),
    )
