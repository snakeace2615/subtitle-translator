from subtitle_translator.models import SubtitleDocument, SubtitleSegment
from subtitle_translator.srt import format_timestamp, render_srt, validate_srt


def test_timestamp_handles_hour_and_millisecond_rounding() -> None:
    assert format_timestamp(3661.2346) == "01:01:01,235"


def test_render_srt_uses_sequential_indices_and_utf8_text() -> None:
    document = SubtitleDocument(
        media_file="课程/Episode.01.mkv",
        source_language="en",
        target_language="zh-CN",
        segments=[
            SubtitleSegment(id=9, start=0, end=1.5, text="第一行\n第二行"),
            SubtitleSegment(id=20, start=2, end=3, text="结束"),
        ],
    )

    value = render_srt(document)

    assert value.startswith("1\n00:00:00,000 --> 00:00:01,500\n第一行\n第二行")
    assert "\n\n2\n00:00:02,000 --> 00:00:03,000\n结束\n" in value
    validate_srt(value, 2)
