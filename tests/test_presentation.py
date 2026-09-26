import re

import pytest

from subtitle_translator.models import SubtitleDocument, SubtitleSegment
from subtitle_translator.presentation import (
    LayoutOptions,
    build_presentation,
    display_width,
    wrap_text,
)
from subtitle_translator.srt import render_srt, validate_srt


def document(text: str, end: float = 8) -> SubtitleDocument:
    return SubtitleDocument(
        media_file="demo.mp4",
        source_language="en",
        target_language="zh-CN",
        segments=[SubtitleSegment(id=7, start=0, end=end, text=text)],
    )


@pytest.mark.parametrize(
    "text",
    [
        "这里是一条需要自动换行的中文字幕，我们希望保留完整内容，并且不会超出画面。" * 3,
        "Apply 0.25mm weathering-pigments with a fine brush. " * 4,
        "第一行\n第二行",
        "喷笔Airbrush压力20PSI，逐层薄喷。" * 5,
    ],
)
def test_layout_bounds_lines_preserves_content_and_source(text: str) -> None:
    source = document(text)
    before = source.model_dump()
    result = build_presentation(source, LayoutOptions(line_width=12, max_lines=2))
    assert source.model_dump() == before
    assert re.sub(r"\s+", "", "".join(c.text for c in result.cues)) == re.sub(r"\s+", "", text)
    assert all(len(c.text.splitlines()) <= 2 for c in result.cues)
    assert all(display_width(line) <= 12 for c in result.cues for line in c.text.splitlines())
    assert result.cues[0].start == 0
    assert result.cues[-1].end == 8
    assert all(a.end == b.start for a, b in zip(result.cues, result.cues[1:]))
    assert all(c.end > c.start for c in result.cues)


def test_quality_report_identifies_short_dense_cue_and_estimated_pages() -> None:
    original = document("Paint the tank.", 2)
    translated = document("这是一条明显过长的中文翻译，包含许多不应添加的内容。" * 5, 2)
    result = build_presentation(translated, source=original)
    issues = {issue.code: issue for issue in result.report.issues}
    assert {"reading_speed", "translation_expansion", "estimated_page_timing"} <= issues.keys()
    assert issues["reading_speed"].segment_ids == [7]
    assert issues["reading_speed"].start_seconds == 0
    assert issues["reading_speed"].end_seconds == 2


def test_long_repeated_cues_are_reported_without_silently_deleting_or_retiming() -> None:
    source = document("谢谢。", 30)
    source.segments.extend(
        [
            SubtitleSegment(id=8, start=30, end=60, text="谢谢。"),
            SubtitleSegment(id=9, start=60, end=90, text="谢谢。"),
        ]
    )
    result = build_presentation(source)
    assert [(c.start, c.end) for c in result.cues] == [(0, 30), (30, 60), (60, 90)]
    assert [i.segment_ids for i in result.report.issues if i.code == "repeated_cues"] == [[7, 8, 9]]
    assert len([i for i in result.report.issues if i.code == "long_duration"]) == 3


def test_overlap_is_reported_and_display_cues_remain_ordered() -> None:
    source = document("这是一段需要分成多个画面的文本。" * 8, 8)
    source.segments.append(SubtitleSegment(id=8, start=1, end=2, text="第二人讲话"))
    result = build_presentation(source)
    assert any(issue.code == "overlap" for issue in result.report.issues)
    assert [item.start for item in result.cues] == sorted(item.start for item in result.cues)
    validate_srt(render_srt(source), len(result.cues))


def test_word_and_decimal_are_not_split_at_available_boundaries() -> None:
    text = "Use a 0.25mm needle and thinner."
    lines = wrap_text(text, 10)
    assert any("0.25mm" in line for line in lines)
    assert any("needle" in line for line in lines)


@pytest.mark.parametrize(
    "timing",
    [
        "00:00:02,000 --> 00:00:01,000",
        "00:00:01,000 --> 00:00:01,000",
        "00:60:00,000 --> 01:01:00,000",
    ],
)
def test_srt_rejects_invalid_display_times(timing: str) -> None:
    with pytest.raises(ValueError):
        validate_srt(f"1\n{timing}\n测试\n", 1)


def test_submillisecond_cue_cannot_be_published() -> None:
    with pytest.raises(ValueError, match="millisecond"):
        render_srt(document("测试", 0.0001))


def test_glossary_targets_that_fit_on_a_line_are_not_split() -> None:
    lines = wrap_text("首先涂装模型的负重轮，再检查履带。", 8, ("负重轮", "履带"))
    assert any("负重轮" in line for line in lines)
    assert all(display_width(line) <= 8 for line in lines)
    assert "".join(lines) == "首先涂装模型的负重轮，再检查履带。"
