from pathlib import Path

from subtitle_translator.batch import validate_translation
from subtitle_translator.config import Settings
from subtitle_translator.glossary import load_glossary
from subtitle_translator.models import SubtitleDocument
from subtitle_translator.presentation import LayoutOptions, QualityReport, build_presentation
from subtitle_translator.service import validate_translation_input
from subtitle_translator.srt import publish_srt, render_cues, sha256_bytes
from subtitle_translator.state import atomic_write_model


def create_preview(job_dir: Path, output_dir: Path, settings: Settings) -> QualityReport:
    job_dir, output_dir = job_dir.expanduser().resolve(), output_dir.expanduser().resolve()
    if any(
        output_dir.is_relative_to(root)
        for root in (
            job_dir,
            settings.data_dir.expanduser().resolve(),
            settings.media_root.expanduser().resolve(),
        )
    ):
        raise ValueError("Preview output must be outside job, data and media directories")
    source_bytes = (job_dir / "source.subtitle.json").read_bytes()
    translated_bytes = (job_dir / f"{settings.target_language}.subtitle.json").read_bytes()
    source = SubtitleDocument.model_validate_json(source_bytes)
    translated = SubtitleDocument.model_validate_json(translated_bytes)
    validate_translation_input(source)
    validate_translation(source, translated, settings.target_language)
    glossary = load_glossary(settings.glossary_path)
    presentation = build_presentation(
        translated,
        LayoutOptions(
            protected_terms=tuple(sorted({term.target for term in glossary.terms})),
            line_width=settings.srt_line_width,
            max_lines=settings.srt_max_lines,
            max_duration=settings.quality_max_duration,
            max_reading_speed=settings.quality_max_reading_speed,
        ),
        source,
    )
    report = presentation.report.model_copy(
        update={
            "source_sha256": sha256_bytes(source_bytes),
            "translation_sha256": sha256_bytes(translated_bytes),
        }
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    atomic_write_model(output_dir / "quality.json", report)
    publish_srt(output_dir / "preview.srt", render_cues(presentation.cues), len(presentation.cues))
    return report
