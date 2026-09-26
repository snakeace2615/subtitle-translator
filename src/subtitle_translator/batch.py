from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import ValidationError

from subtitle_translator.config import Settings
from subtitle_translator.glossary import GlossaryDocument, load_glossary
from subtitle_translator.llm_client import (
    CONTEXT_STRATEGY,
    OUTPUT_CONTRACT_VERSION,
    PROMPT_VERSION,
    DeepSeekClient,
    validate_deepseek_settings,
)
from subtitle_translator.models import SubtitleDocument, TranslatedItem, TranslationRequest
from subtitle_translator.presentation import LayoutOptions, QualityReport, build_presentation
from subtitle_translator.service import (
    TranslationClient,
    TranslationInputError,
    noncompliant_indexes,
    translate_document,
    validate_translation_input,
)
from subtitle_translator.srt import (
    publish_srt,
    render_cues,
    sha256_bytes,
    sha256_file,
    validate_srt,
)
from subtitle_translator.state import (
    ExportReference,
    ExtractionState,
    ProfileReference,
    StateError,
    TaskLock,
    TranslationProgress,
    TranslationState,
    atomic_write_model,
    utc_now,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class BatchSummary:
    discovered: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    busy: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class Candidate:
    job_id: str
    job_dir: Path
    source_path: Path
    source_sha256: str
    source_document: SubtitleDocument
    media_relative_path: PurePosixPath
    media_path: Path
    srt_relative_path: PurePosixPath
    srt_path: Path
    state_path: Path
    output_path: Path
    progress_path: Path
    previous_state: TranslationState | None
    state_error: str | None = None


class JobError(RuntimeError):
    def __init__(
        self,
        stage: Literal["input", "translation", "export"],
        message: str,
        export_reference: ExportReference | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.export_reference = export_reference


class BatchTranslator:
    def __init__(
        self,
        settings: Settings,
        client_factory: Callable[[Settings], TranslationClient] | None = None,
    ) -> None:
        self.settings = settings
        self.data_root = settings.data_dir.expanduser().resolve()
        self.media_root = settings.media_root.expanduser().resolve()
        if client_factory is None:
            validate_deepseek_settings(settings)
        self.client_factory = client_factory or DeepSeekClient
        self.glossary = load_glossary(settings.glossary_path)
        self.profile = build_profile(settings, self.glossary)
        self.layout = LayoutOptions(
            protected_terms=tuple(sorted({term.target for term in self.glossary.terms})),
            line_width=settings.srt_line_width,
            max_lines=settings.srt_max_lines,
            max_duration=settings.quality_max_duration,
            max_reading_speed=settings.quality_max_reading_speed,
        )

    def run(self) -> BatchSummary:
        self._prepare_directories()
        job_dirs = self._job_directories()
        summary = BatchSummary(discovered=len(job_dirs))
        candidates: list[Candidate] = []

        for job_dir in job_dirs:
            try:
                candidate = self._load_candidate(job_dir)
            except JobError as exc:
                LOGGER.error("Rejected job %s: %s", job_dir.name, exc)
                try:
                    self._record_input_failure(job_dir, exc)
                except Exception:
                    LOGGER.exception("Could not record rejected job %s", job_dir.name)
                summary.failed += 1
                continue
            if candidate is None:
                summary.skipped += 1
                continue
            candidates.append(candidate)

        collisions = self._find_collisions(candidates)
        for candidate in candidates:
            if candidate.job_id in collisions:
                error = JobError(
                    "export",
                    f"SRT naming collision for {candidate.srt_relative_path.as_posix()}",
                )
                LOGGER.error("Rejected job %s: %s", candidate.job_id, error)
                try:
                    self._record_candidate_failure(candidate, error)
                except Exception:
                    LOGGER.exception("Could not record collision for %s", candidate.job_id)
                summary.failed += 1
                continue

            outcome = self._process_candidate_safely(candidate)
            setattr(summary, outcome, getattr(summary, outcome) + 1)

        return summary

    def run_one(self, media_relative_path: str | None = None) -> BatchSummary:
        """Translate exactly one explicitly selected or automatically chosen job."""
        self._prepare_directories()
        job_dirs = self._job_directories()
        summary = BatchSummary(discovered=1)

        if media_relative_path is None:
            candidate = self._select_automatic_candidate(job_dirs)
            outcome = self._process_candidate_safely(candidate)
            setattr(summary, outcome, 1)
            return summary

        selector = safe_relative_path(media_relative_path).as_posix()
        target_job_dir = self._find_job_dir_by_media_path(job_dirs, selector)
        try:
            candidate = self._load_candidate(target_job_dir)
        except JobError as exc:
            LOGGER.error("Rejected selected job %s: %s", target_job_dir.name, exc)
            try:
                self._record_input_failure(target_job_dir, exc)
            except Exception:
                LOGGER.exception("Could not record rejected job %s", target_job_dir.name)
            summary.failed = 1
            return summary

        if candidate is None:
            summary.skipped = 1
            return summary
        if self._has_external_collision(candidate, job_dirs):
            error = JobError(
                "export",
                f"SRT naming collision for {candidate.srt_relative_path.as_posix()}",
            )
            self._record_candidate_failure(candidate, error)
            summary.failed = 1
            return summary

        outcome = self._process_candidate_safely(candidate)
        setattr(summary, outcome, 1)
        return summary

    def _find_job_dir_by_media_path(self, job_dirs: list[Path], selector: str) -> Path:
        matching_job_dirs: list[Path] = []
        for job_dir in job_dirs:
            extraction_path = job_dir / "extract.state.json"
            try:
                extraction = ExtractionState.model_validate_json(
                    extraction_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                continue
            if extraction.source.relative_path == selector:
                matching_job_dirs.append(job_dir)

        if not matching_job_dirs:
            raise ValueError(f"No extraction job found for media path: {selector}")
        if len(matching_job_dirs) > 1:
            raise ValueError(f"Multiple extraction jobs found for media path: {selector}")
        return matching_job_dirs[0]

    def _select_automatic_candidate(self, job_dirs: list[Path]) -> Candidate:
        candidates: list[Candidate] = []
        for job_dir in job_dirs:
            try:
                candidate = self._load_candidate(job_dir)
            except JobError as exc:
                LOGGER.warning("Skipped invalid translation input for %s: %s", job_dir.name, exc)
                continue
            if candidate is not None and candidate.state_error is None:
                candidates.append(candidate)

        collisions = self._find_collisions(candidates)
        for candidate in candidates:
            if candidate.job_id in collisions or self._can_skip(
                candidate, candidate.previous_state
            ):
                continue
            if self._task_lock(candidate).is_active():
                continue
            previous = candidate.previous_state
            same_series = bool(
                previous
                and previous.source_sha256 == candidate.source_sha256
                and previous.profile.fingerprint == self.profile.fingerprint
                and previous.validation_fingerprint == self.glossary.validation_fingerprint
                and previous.presentation_fingerprint == self.layout.fingerprint
            )
            attempts_exhausted = bool(
                same_series
                and previous is not None
                and previous.status == "failed"
                and previous.attempt >= self.settings.max_attempts
            )
            if not attempts_exhausted:
                return candidate
        raise ValueError("No pending eligible extraction job found")

    def _task_lock(self, candidate: Candidate) -> TaskLock:
        return TaskLock(
            self.data_root
            / "locks"
            / f"{candidate.job_id}.translate.{self.settings.target_language}.lock",
            self.settings.stale_lock_seconds,
        )

    def _process_candidate_safely(
        self, candidate: Candidate
    ) -> Literal["completed", "skipped", "failed", "busy"]:
        try:
            return self._process_candidate(candidate)
        except Exception:
            LOGGER.exception("Unexpected task failure for %s", candidate.job_id)
            return "failed"

    def _job_directories(self) -> list[Path]:
        return sorted(path for path in (self.data_root / "jobs").glob("*/*") if path.is_dir())

    def _prepare_directories(self) -> None:
        for name in ("jobs", "locks", "temp", "failed"):
            (self.data_root / name).mkdir(parents=True, exist_ok=True)

    def _load_candidate(self, job_dir: Path) -> Candidate | None:
        job_id = job_dir.name
        if job_dir.parent.name != job_id[:2]:
            raise JobError("input", "Job shard does not match job ID")

        extraction_path = job_dir / "extract.state.json"
        try:
            extraction = ExtractionState.model_validate_json(
                extraction_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise JobError("input", f"Invalid extraction state: {short_error(exc)}") from exc
        if extraction.job_id != job_id:
            raise JobError("input", "Extraction state job ID does not match directory")
        if extraction.status != "complete":
            LOGGER.info("Skipped extraction that is not complete: %s", job_id)
            return None
        if extraction.output != "source.subtitle.json":
            raise JobError("input", "Unexpected extraction output filename")

        source_path = job_dir / "source.subtitle.json"
        try:
            source_document = SubtitleDocument.model_validate_json(
                source_path.read_text(encoding="utf-8")
            )
            validate_translation_input(source_document)
            source_sha256 = sha256_file(source_path)
        except (OSError, ValidationError, TranslationInputError) as exc:
            raise JobError("input", f"Invalid source subtitle: {short_error(exc)}") from exc

        relative_path = safe_relative_path(extraction.source.relative_path)
        if source_document.media_file != relative_path.as_posix():
            raise JobError("input", "Source subtitle media_file does not match extraction state")
        media_path = safe_join(self.media_root, relative_path)
        if not media_path.is_file():
            raise JobError("input", f"Source media file not found: {relative_path.as_posix()}")
        srt_relative_path = relative_path.with_suffix(".srt")
        srt_path = safe_join(self.media_root, srt_relative_path)

        state_path = job_dir / self._state_filename
        previous_state: TranslationState | None = None
        state_error: str | None = None
        if state_path.exists():
            try:
                previous_state = TranslationState.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
                if previous_state.job_id != job_id:
                    raise ValueError("translation state job ID does not match directory")
            except (OSError, ValidationError, ValueError) as exc:
                state_error = f"Invalid translation state: {short_error(exc)}"

        return Candidate(
            job_id=job_id,
            job_dir=job_dir,
            source_path=source_path,
            source_sha256=source_sha256,
            source_document=source_document,
            media_relative_path=relative_path,
            media_path=media_path,
            srt_relative_path=srt_relative_path,
            srt_path=srt_path,
            state_path=state_path,
            output_path=job_dir / self._output_filename,
            progress_path=job_dir / self._progress_filename,
            previous_state=previous_state,
            state_error=state_error,
        )

    def _find_collisions(self, candidates: list[Candidate]) -> set[str]:
        destinations: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            destinations[candidate.srt_relative_path.as_posix().casefold()].append(candidate)
        return {
            candidate.job_id
            for group in destinations.values()
            if len(group) > 1
            for candidate in group
        }

    def _has_external_collision(self, target: Candidate, job_dirs: list[Path]) -> bool:
        target_key = target.srt_relative_path.as_posix().casefold()
        for job_dir in job_dirs:
            if job_dir == target.job_dir:
                continue
            try:
                other = self._load_candidate(job_dir)
            except JobError:
                continue
            if other is not None and other.srt_relative_path.as_posix().casefold() == target_key:
                return True
        return False

    def _process_candidate(
        self, candidate: Candidate
    ) -> Literal["completed", "skipped", "failed", "busy"]:
        lock = self._task_lock(candidate)
        if not lock.acquire():
            LOGGER.info("Translation is already locked: %s", candidate.job_id)
            return "busy"

        failure_basis = candidate.previous_state
        current_attempt = 1
        try:
            if candidate.state_error:
                self._write_failure(candidate, None, JobError("input", candidate.state_error), 1)
                return "failed"

            previous = candidate.previous_state
            if previous is not None and previous.status == "processing":
                recovered = previous.model_copy(
                    update={
                        "status": "failed",
                        "completed_at": utc_now(),
                        "error": StateError(
                            stage="translation",
                            type="InterruptedProcessing",
                            message="Recovered processing state without an active lock",
                        ),
                    }
                )
                atomic_write_model(candidate.state_path, recovered)
                previous = recovered
                failure_basis = recovered

            if self._can_skip(candidate, previous, require_current_validation=False):
                if previous.validation_fingerprint != self.glossary.validation_fingerprint:
                    validated = previous.model_copy(
                        update={"validation_fingerprint": self.glossary.validation_fingerprint}
                    )
                    atomic_write_model(candidate.state_path, validated)
                LOGGER.info("Skipped completed translation: %s", candidate.job_id)
                return "skipped"

            same_series = bool(
                previous
                and previous.source_sha256 == candidate.source_sha256
                and previous.profile.fingerprint == self.profile.fingerprint
                and previous.validation_fingerprint == self.glossary.validation_fingerprint
                and previous.presentation_fingerprint == self.layout.fingerprint
            )
            if (
                same_series
                and previous is not None
                and previous.status == "failed"
                and previous.attempt >= self.settings.max_attempts
            ):
                LOGGER.error("Maximum attempts reached for translation: %s", candidate.job_id)
                return "failed"

            attempt = previous.attempt + 1 if same_series and previous else 1
            current_attempt = attempt
            cached_document = self._read_reusable_output(candidate, previous)
            self._ensure_destination_replaceable(candidate, previous)

            processing = TranslationState(
                job_id=candidate.job_id,
                target_language=self.settings.target_language,
                source_sha256=candidate.source_sha256,
                profile=self.profile,
                validation_fingerprint=self.glossary.validation_fingerprint,
                presentation_fingerprint=self.layout.fingerprint,
                status="processing",
                attempt=attempt,
                output=self._output_filename,
                output_sha256=(previous.output_sha256 if cached_document and previous else None),
                export=self._pending_export(candidate, previous),
                started_at=utc_now(),
            )
            atomic_write_model(candidate.state_path, processing)
            failure_basis = processing

            try:
                needs_repair = cached_document is not None and bool(
                    noncompliant_indexes(
                        candidate.source_document.segments, cached_document.segments, self.glossary
                    )
                )
                translated_document = (
                    self._translate(candidate, lock, cached_document)
                    if cached_document is None or needs_repair
                    else cached_document
                )
                if sha256_file(candidate.source_path) != candidate.source_sha256:
                    raise JobError("translation", "Source subtitle changed during translation")
                if cached_document is None or needs_repair:
                    atomic_write_model(candidate.output_path, translated_document)
                output_sha256 = sha256_file(candidate.output_path)
                candidate.progress_path.unlink(missing_ok=True)
                processing = processing.model_copy(update={"output_sha256": output_sha256})
                atomic_write_model(candidate.state_path, processing)
                failure_basis = processing
            except JobError:
                raise
            except Exception as exc:
                raise JobError("translation", short_error(exc)) from exc

            export_reference: ExportReference | None = None
            if self.settings.export_srt:
                try:
                    if sha256_file(candidate.source_path) != candidate.source_sha256:
                        raise RuntimeError("Source subtitle changed before SRT export")
                    self._ensure_destination_replaceable(candidate, previous)
                    presentation = build_presentation(
                        translated_document, self.layout, candidate.source_document
                    )
                    report = presentation.report.model_copy(
                        update={
                            "source_sha256": candidate.source_sha256,
                            "translation_sha256": output_sha256,
                        }
                    )
                    atomic_write_model(self._quality_path(candidate), report)
                    if report.issues:
                        LOGGER.warning(
                            "Subtitle quality issues job=%s count=%d report=%s",
                            candidate.job_id,
                            len(report.issues),
                            self._quality_path(candidate),
                        )
                    srt_value = render_cues(presentation.cues)
                    expected_sha256 = sha256_bytes(srt_value.encode("utf-8"))
                    pending_export = processing.export
                    processing = processing.model_copy(
                        update={
                            "export": ExportReference(
                                relative_path=candidate.srt_relative_path.as_posix(),
                                sha256=expected_sha256,
                                layout_fingerprint=self.layout.fingerprint,
                                cue_count=len(presentation.cues),
                                previous_sha256=(
                                    pending_export.previous_sha256 if pending_export else None
                                ),
                                status="pending",
                            )
                        }
                    )
                    atomic_write_model(candidate.state_path, processing)
                    failure_basis = processing
                    export_sha256 = publish_srt(
                        candidate.srt_path,
                        srt_value,
                        len(presentation.cues),
                    )
                    if export_sha256 != expected_sha256:
                        raise RuntimeError("Published SRT hash does not match prepared content")
                    export_reference = ExportReference(
                        relative_path=candidate.srt_relative_path.as_posix(),
                        sha256=export_sha256,
                        layout_fingerprint=self.layout.fingerprint,
                        cue_count=len(presentation.cues),
                        status="complete",
                    )
                except Exception as exc:
                    raise JobError(
                        "export",
                        short_error(exc),
                        processing.export,
                    ) from exc

            complete = processing.model_copy(
                update={
                    "status": "complete",
                    "export": export_reference,
                    "completed_at": utc_now(),
                    "error": None,
                }
            )
            atomic_write_model(candidate.state_path, complete)
            LOGGER.info("Completed translation: %s", candidate.job_id)
            return "completed"
        except JobError as exc:
            self._write_failure(candidate, failure_basis, exc, current_attempt)
            LOGGER.error("Translation failed for %s: %s", candidate.job_id, exc)
            return "failed"
        except Exception as exc:
            error = JobError("translation", short_error(exc))
            try:
                self._write_failure(candidate, failure_basis, error, current_attempt)
            except Exception:
                LOGGER.exception("Could not record task failure for %s", candidate.job_id)
            LOGGER.exception("Unexpected translation failure for %s", candidate.job_id)
            return "failed"
        finally:
            lock.release()

    def _translate(
        self, candidate: Candidate, lock: TaskLock, cached: SubtitleDocument | None = None
    ) -> SubtitleDocument:
        client = self.client_factory(self.settings)
        request = TranslationRequest(
            document=candidate.source_document,
            target_language=self.settings.target_language,
            glossary=self.glossary,
        )
        initial_translations = self._read_progress(candidate)
        if cached is not None and len(initial_translations) < len(cached.segments):
            initial_translations = [
                TranslatedItem(id=item.id, text=item.text) for item in cached.segments
            ]

        def save_progress(translations: list[TranslatedItem]) -> None:
            lock.heartbeat()
            progress = TranslationProgress(
                job_id=candidate.job_id,
                target_language=self.settings.target_language,
                source_sha256=candidate.source_sha256,
                profile_fingerprint=self.profile.fingerprint,
                validation_fingerprint=self.glossary.validation_fingerprint,
                batch_size=self.settings.batch_size,
                completed_ids=[item.id for item in translations],
                translations=translations,
                updated_at=utc_now(),
            )
            atomic_write_model(candidate.progress_path, progress)

        document = asyncio.run(
            translate_document(
                request,
                self.settings,
                client=client,
                progress=lock.heartbeat,
                initial_translations=initial_translations,
                batch_completed=save_progress,
            )
        )
        validate_translation(candidate.source_document, document, self.settings.target_language)
        return document

    def _read_progress(self, candidate: Candidate) -> list[TranslatedItem]:
        if not candidate.progress_path.is_file():
            return []
        try:
            progress = TranslationProgress.model_validate_json(
                candidate.progress_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            return []
        if (
            progress.job_id != candidate.job_id
            or progress.target_language != self.settings.target_language
            or progress.source_sha256 != candidate.source_sha256
            or progress.profile_fingerprint != self.profile.fingerprint
            or progress.batch_size != self.settings.batch_size
        ):
            return []
        expected_ids = [item.id for item in candidate.source_document.segments]
        if (
            len(progress.completed_ids) > len(expected_ids)
            or progress.completed_ids != expected_ids[: len(progress.completed_ids)]
            or (
                len(progress.completed_ids) != len(expected_ids)
                and len(progress.completed_ids) % self.settings.batch_size
            )
        ):
            return []
        return progress.translations

    def _can_skip(
        self,
        candidate: Candidate,
        state: TranslationState | None,
        *,
        require_current_validation: bool = True,
    ) -> bool:
        if (
            state is None
            or state.status != "complete"
            or (
                require_current_validation
                and state.validation_fingerprint != self.glossary.validation_fingerprint
            )
            or state.source_sha256 != candidate.source_sha256
            or state.profile.fingerprint != self.profile.fingerprint
        ):
            return False
        document = self._read_reusable_output(candidate, state)
        if document is None or noncompliant_indexes(
            candidate.source_document.segments, document.segments, self.glossary
        ):
            return False
        if not self.settings.export_srt:
            return True
        export = state.export
        if (
            export is None
            or export.status != "complete"
            or export.layout_fingerprint != self.layout.fingerprint
            or export.cue_count is None
            or export.relative_path != candidate.srt_relative_path.as_posix()
            or export.sha256 is None
            or not candidate.srt_path.is_file()
            or sha256_file(candidate.srt_path) != export.sha256
        ):
            return False
        try:
            report = QualityReport.model_validate_json(
                self._quality_path(candidate).read_text(encoding="utf-8")
            )
            if (
                report.layout_fingerprint != self.layout.fingerprint
                or report.source_sha256 != candidate.source_sha256
                or report.translation_sha256 != state.output_sha256
                or report.output_cues != export.cue_count
            ):
                return False
            validate_srt(candidate.srt_path.read_text(encoding="utf-8"), export.cue_count)
        except (OSError, ValueError):
            return False
        return True

    def _quality_path(self, candidate: Candidate) -> Path:
        return candidate.job_dir / f"translate.{self.settings.target_language}.quality.json"

    def _read_reusable_output(
        self, candidate: Candidate, state: TranslationState | None
    ) -> SubtitleDocument | None:
        if (
            state is None
            or state.source_sha256 != candidate.source_sha256
            or state.profile.fingerprint != self.profile.fingerprint
            or state.output != self._output_filename
            or state.output_sha256 is None
            or not candidate.output_path.is_file()
            or sha256_file(candidate.output_path) != state.output_sha256
        ):
            return None
        try:
            document = SubtitleDocument.model_validate_json(
                candidate.output_path.read_text(encoding="utf-8")
            )
            validate_translation(
                candidate.source_document,
                document,
                self.settings.target_language,
            )
        except (OSError, ValidationError, ValueError):
            return None
        return document

    def _ensure_destination_replaceable(
        self, candidate: Candidate, state: TranslationState | None
    ) -> None:
        if not self.settings.export_srt or not candidate.srt_path.exists():
            return
        if not candidate.srt_path.is_file():
            raise JobError("export", f"SRT destination is not a file: {candidate.srt_path}")
        managed = self._managed_destination_hash(candidate, state) is not None
        if not managed and not self.settings.overwrite_unmanaged_srt:
            raise JobError(
                "export",
                f"Refusing to overwrite unmanaged or modified SRT: "
                f"{candidate.srt_relative_path.as_posix()}",
            )

    def _pending_export(
        self, candidate: Candidate, previous: TranslationState | None
    ) -> ExportReference | None:
        if not self.settings.export_srt:
            return None
        previous_sha = self._managed_destination_hash(candidate, previous)
        return ExportReference(
            relative_path=candidate.srt_relative_path.as_posix(),
            previous_sha256=previous_sha,
            status="pending",
        )

    def _managed_destination_hash(
        self, candidate: Candidate, state: TranslationState | None
    ) -> str | None:
        if state is None or state.export is None or not candidate.srt_path.is_file():
            return None
        export = state.export
        if export.relative_path != candidate.srt_relative_path.as_posix():
            return None
        current_sha256 = sha256_file(candidate.srt_path)
        known_hashes = {export.sha256, export.previous_sha256}
        return current_sha256 if current_sha256 in known_hashes else None

    def _write_failure(
        self,
        candidate: Candidate,
        previous: TranslationState | None,
        error: JobError,
        attempt: int,
    ) -> None:
        previous_export = previous.export if previous is not None else None
        managed_sha256 = self._managed_destination_hash(candidate, previous)
        export: ExportReference | None = None
        if managed_sha256 is not None:
            export = ExportReference(
                relative_path=candidate.srt_relative_path.as_posix(),
                sha256=managed_sha256,
                status="complete",
            )
        elif self.settings.export_srt and error.stage == "export":
            failure_export = error.export_reference or previous_export
            export = ExportReference(
                relative_path=candidate.srt_relative_path.as_posix(),
                sha256=failure_export.sha256 if failure_export else None,
                previous_sha256=(failure_export.previous_sha256 if failure_export else None),
                status="failed",
            )
        state = TranslationState(
            job_id=candidate.job_id,
            target_language=self.settings.target_language,
            source_sha256=candidate.source_sha256,
            profile=self.profile,
            validation_fingerprint=self.glossary.validation_fingerprint,
            presentation_fingerprint=self.layout.fingerprint,
            status="failed",
            attempt=max(attempt, 1),
            output=self._output_filename,
            output_sha256=(
                previous.output_sha256
                if previous is not None
                and previous.source_sha256 == candidate.source_sha256
                and previous.profile.fingerprint == self.profile.fingerprint
                else None
            ),
            export=export,
            started_at=utc_now(),
            completed_at=utc_now(),
            error=StateError(
                stage=error.stage,
                type=type(error).__name__,
                message=str(error)[:300],
            ),
        )
        atomic_write_model(candidate.state_path, state)

    def _record_candidate_failure(self, candidate: Candidate, error: JobError) -> None:
        lock = TaskLock(
            self.data_root
            / "locks"
            / f"{candidate.job_id}.translate.{self.settings.target_language}.lock",
            self.settings.stale_lock_seconds,
        )
        if not lock.acquire():
            return
        try:
            previous = candidate.previous_state
            attempt = previous.attempt + 1 if previous else 1
            self._write_failure(candidate, previous, error, attempt)
        finally:
            lock.release()

    def _record_input_failure(self, job_dir: Path, error: JobError) -> None:
        source_path = job_dir / "source.subtitle.json"
        source_sha256 = sha256_file(source_path) if source_path.is_file() else "sha256:unavailable"
        state_path = job_dir / self._state_filename
        previous: TranslationState | None = None
        if state_path.is_file():
            try:
                previous = TranslationState.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError):
                pass
        lock = TaskLock(
            self.data_root
            / "locks"
            / f"{job_dir.name}.translate.{self.settings.target_language}.lock",
            self.settings.stale_lock_seconds,
        )
        if not lock.acquire():
            return
        try:
            state = TranslationState(
                job_id=job_dir.name,
                target_language=self.settings.target_language,
                source_sha256=source_sha256,
                profile=self.profile,
                validation_fingerprint=self.glossary.validation_fingerprint,
                presentation_fingerprint=self.layout.fingerprint,
                status="failed",
                attempt=previous.attempt + 1 if previous else 1,
                output=self._output_filename,
                output_sha256=previous.output_sha256 if previous else None,
                export=previous.export if previous else None,
                started_at=utc_now(),
                completed_at=utc_now(),
                error=StateError(
                    stage="input",
                    type=type(error).__name__,
                    message=str(error)[:300],
                ),
            )
            atomic_write_model(state_path, state)
        finally:
            lock.release()

    @property
    def _state_filename(self) -> str:
        return f"translate.{self.settings.target_language}.state.json"

    @property
    def _output_filename(self) -> str:
        return f"{self.settings.target_language}.subtitle.json"

    @property
    def _progress_filename(self) -> str:
        return f"translate.{self.settings.target_language}.progress.json"


def build_profile(settings: Settings, glossary: GlossaryDocument) -> ProfileReference:
    value = {
        "profile_version": settings.profile_version,
        "target_language": settings.target_language,
        "glossary": json.loads(glossary.translation_canonical_json()),
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "thinking": settings.llm_thinking,
        "max_output_tokens": settings.llm_max_output_tokens,
        "prompt_version": PROMPT_VERSION,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "batch_size": settings.batch_size,
        "context_strategy": CONTEXT_STRATEGY,
        "output_contract_version": OUTPUT_CONTRACT_VERSION,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ProfileReference(
        fingerprint=sha256_bytes(canonical.encode("utf-8")),
        version=settings.profile_version,
        model=settings.llm_model,
    )


def validate_translation(
    source: SubtitleDocument,
    translated: SubtitleDocument,
    target_language: str,
) -> None:
    if translated.schema_version != source.schema_version:
        raise ValueError("Translation changed schema version")
    if translated.media_file != source.media_file:
        raise ValueError("Translation changed media_file")
    if translated.source_language != source.source_language:
        raise ValueError("Translation changed source language")
    if translated.target_language != target_language:
        raise ValueError("Translation has the wrong target language")
    if len(translated.segments) != len(source.segments):
        raise ValueError("Translation changed segment count")
    for source_segment, translated_segment in zip(source.segments, translated.segments):
        if (
            translated_segment.id != source_segment.id
            or translated_segment.start != source_segment.start
            or translated_segment.end != source_segment.end
        ):
            raise ValueError(f"Translation changed segment {source_segment.id} metadata")


def safe_relative_path(value: str) -> PurePosixPath:
    if "\\" in value:
        raise JobError("input", "Media relative path must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise JobError("input", "Unsafe media relative path")
    return path


def safe_join(root: Path, relative_path: PurePosixPath) -> Path:
    target = root.joinpath(*relative_path.parts).resolve()
    if not target.is_relative_to(root):
        raise JobError("input", "Media path escapes configured media root")
    return target


def short_error(error: Exception) -> str:
    value = " ".join(str(error).split())
    return f"{type(error).__name__}: {value}"[:300]
