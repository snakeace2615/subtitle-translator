import json
import os
import time
from pathlib import Path

import pytest

from subtitle_translator.batch import BatchTranslator
from subtitle_translator.config import Settings
from subtitle_translator.models import TranslatedItem
from subtitle_translator.state import TranslationState


class FakeClientFactory:
    def __init__(self, fail_text: str | None = None, omit_last: bool = False) -> None:
        self.calls = 0
        self.fail_text = fail_text
        self.omit_last = omit_last

    def __call__(self, settings):
        return self

    async def translate_batch(self, segments, source_language, target_language, glossary):
        self.calls += 1
        if self.fail_text and any(self.fail_text in segment.text for segment in segments):
            raise RuntimeError("simulated LLM failure")
        items = [
            TranslatedItem(id=segment.id, text=f"中文：{segment.text}") for segment in segments
        ]
        return items[:-1] if self.omit_last else items


def make_settings(data_root: Path, media_root: Path, **overrides) -> Settings:
    values = {
        "data_dir": data_root,
        "media_root": media_root,
        "media_mount_source": "",
        "target_language": "zh-CN",
        "export_srt": True,
        "batch_size": 20,
    }
    values.update(overrides)
    return Settings(**values)


def test_missing_api_key_fails_before_creating_or_modifying_task_state(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    settings = Settings(
        _env_file=None,
        data_dir=data_root,
        media_root=media_root,
        media_mount_source="",
        glossary_path=tmp_path / "missing-glossary.json",
        llm_api_key="",
    )

    with pytest.raises(ValueError, match="API key is missing"):
        BatchTranslator(settings)

    assert not data_root.exists()


def create_job(
    data_root: Path,
    media_root: Path,
    job_id: str,
    relative_path: str,
    text: str = "Hello",
    status: str = "complete",
) -> Path:
    job_dir = data_root / "jobs" / job_id[:2] / job_id
    job_dir.mkdir(parents=True)
    media_path = media_root.joinpath(*Path(relative_path).parts)
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"video")
    extraction_state = {
        "version": 1,
        "job_id": job_id,
        "source": {"relative_path": relative_path},
        "status": status,
        "output": "source.subtitle.json",
    }
    source_document = {
        "schema_version": "subtitle-document/v1",
        "media_file": relative_path,
        "source_language": "en",
        "segments": [{"id": 0, "start": 1.25, "end": 2.5, "text": text}],
    }
    (job_dir / "extract.state.json").write_text(json.dumps(extraction_state), encoding="utf-8")
    (job_dir / "source.subtitle.json").write_text(json.dumps(source_document), encoding="utf-8")
    return job_dir


def test_completed_job_is_published_then_skipped_without_llm(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_id = "ab" + "1" * 62
    job_dir = create_job(data_root, media_root, job_id, "课程/Painting Panther.mp4")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root)

    first = BatchTranslator(settings, client).run()
    second = BatchTranslator(settings, client).run()

    assert first.completed == 1
    assert second.skipped == 1
    assert client.calls == 1
    assert (media_root / "课程/Painting Panther.srt").read_text(encoding="utf-8") == (
        "1\n00:00:01,250 --> 00:00:02,500\n中文：Hello\n"
    )
    state = TranslationState.model_validate_json(
        (job_dir / "translate.zh-CN.state.json").read_text(encoding="utf-8")
    )
    assert state.status == "complete"
    assert state.export is not None and state.export.status == "complete"


def test_run_one_only_modifies_selected_video(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    untouched_job = create_job(data_root, media_root, "ab" + "11" * 31, "first.mp4")
    selected_job = create_job(
        data_root,
        media_root,
        "cd" + "22" * 31,
        "课程/second.mp4",
    )
    client = FakeClientFactory()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run_one(
        "课程/second.mp4"
    )

    assert summary.discovered == 1
    assert summary.completed == 1
    assert client.calls == 1
    assert (selected_job / "translate.zh-CN.state.json").is_file()
    assert (media_root / "课程/second.srt").is_file()
    assert not (untouched_job / "translate.zh-CN.state.json").exists()
    assert not (media_root / "first.srt").exists()


def test_run_one_without_video_name_selects_first_pending_job(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    first_job = create_job(data_root, media_root, "ab" + "66" * 31, "first.mp4")
    second_job = create_job(data_root, media_root, "cd" + "77" * 31, "second.mp4")
    client = FakeClientFactory()
    translator = BatchTranslator(make_settings(data_root, media_root), client)

    assert translator.run_one("first.mp4").completed == 1
    first_state_path = first_job / "translate.zh-CN.state.json"
    first_state = first_state_path.read_bytes()

    summary = translator.run_one()

    assert summary.discovered == 1
    assert summary.completed == 1
    assert client.calls == 2
    assert (second_job / "translate.zh-CN.state.json").is_file()
    assert (media_root / "second.srt").is_file()
    assert first_state_path.read_bytes() == first_state

    with pytest.raises(ValueError, match="No pending eligible extraction job found"):
        translator.run_one()


def test_run_one_without_video_name_skips_active_lock(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    locked_job_id = "ab" + "88" * 31
    locked_job = create_job(data_root, media_root, locked_job_id, "locked.mp4")
    selected_job = create_job(data_root, media_root, "cd" + "99" * 31, "selected.mp4")
    lock_path = data_root / "locks" / f"{locked_job_id}.translate.zh-CN.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{}", encoding="utf-8")
    client = FakeClientFactory()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run_one()

    assert summary.completed == 1
    assert client.calls == 1
    assert not (locked_job / "translate.zh-CN.state.json").exists()
    assert (selected_job / "translate.zh-CN.state.json").is_file()
    assert lock_path.read_text(encoding="utf-8") == "{}"


def test_run_one_rejects_missing_video_without_llm(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "33" * 31, "existing.mp4")
    client = FakeClientFactory()

    with pytest.raises(ValueError, match="No extraction job found"):
        BatchTranslator(make_settings(data_root, media_root), client).run_one("missing.mp4")

    assert client.calls == 0


def test_run_one_checks_collision_without_modifying_other_job(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    selected_job = create_job(data_root, media_root, "ab" + "44" * 31, "demo.mp4")
    other_job = create_job(data_root, media_root, "cd" + "55" * 31, "demo.mkv")
    client = FakeClientFactory()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run_one("demo.mp4")

    assert summary.failed == 1
    assert client.calls == 0
    assert (selected_job / "translate.zh-CN.state.json").is_file()
    assert not (other_job / "translate.zh-CN.state.json").exists()


def test_profile_change_retranslates_and_replaces_managed_srt(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "2" * 62, "demo.mp4")
    client = FakeClientFactory()

    assert BatchTranslator(make_settings(data_root, media_root), client).run().completed == 1
    changed = make_settings(data_root, media_root, profile_version=2)
    assert BatchTranslator(changed, client).run().completed == 1
    assert client.calls == 2


def test_source_and_glossary_changes_invalidate_completed_translation(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "d" * 62, "demo.mp4")
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text('{"weathering":"旧化"}', encoding="utf-8")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root, glossary_path=glossary_path)

    assert BatchTranslator(settings, client).run().completed == 1
    source_path = job_dir / "source.subtitle.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["segments"][0]["text"] = "Hello again"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    assert BatchTranslator(settings, client).run().completed == 1

    glossary_path.write_text('{"weathering":"做旧"}', encoding="utf-8")
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.calls == 3


def test_modified_internal_translation_hash_forces_retranslation(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "e" * 62, "demo.mp4")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root)

    assert BatchTranslator(settings, client).run().completed == 1
    output_path = job_dir / "zh-CN.subtitle.json"
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["segments"][0]["text"] = "用户修改"
    output_path.write_text(json.dumps(output), encoding="utf-8")

    assert BatchTranslator(settings, client).run().completed == 1
    assert client.calls == 2


def test_export_failure_reuses_valid_translation_without_llm(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "3" * 62, "demo.mp4")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root)
    from subtitle_translator import batch as batch_module

    real_publish = batch_module.publish_srt

    def failed_publish(*args, **kwargs):
        raise PermissionError("SMB is read-only")

    monkeypatch.setattr(batch_module, "publish_srt", failed_publish)
    first = BatchTranslator(settings, client).run()
    assert first.failed == 1
    assert client.calls == 1
    assert (job_dir / "zh-CN.subtitle.json").is_file()

    monkeypatch.setattr(batch_module, "publish_srt", real_publish)
    second = BatchTranslator(settings, client).run()
    assert second.completed == 1
    assert client.calls == 1


def test_state_write_failure_after_srt_publish_recovers_without_llm(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_id = "ab" + "c" * 62
    create_job(data_root, media_root, job_id, "demo.mp4")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root)
    from subtitle_translator import batch as batch_module

    real_atomic_write = batch_module.atomic_write_model
    failed_once = False

    def interrupt_complete_state(path, model):
        nonlocal failed_once
        if isinstance(model, TranslationState) and model.status == "complete" and not failed_once:
            failed_once = True
            raise OSError("simulated state write interruption")
        real_atomic_write(path, model)

    monkeypatch.setattr(batch_module, "atomic_write_model", interrupt_complete_state)

    first = BatchTranslator(settings, client).run()
    assert first.failed == 1
    assert client.calls == 1
    assert (media_root / "demo.srt").is_file()

    second = BatchTranslator(settings, client).run()
    assert second.completed == 1
    assert client.calls == 1


def test_missing_llm_segment_rejects_output(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "4" * 62, "demo.mp4")
    client = FakeClientFactory(omit_last=True)

    summary = BatchTranslator(make_settings(data_root, media_root), client).run()

    assert summary.failed == 1
    assert not (job_dir / "zh-CN.subtitle.json").exists()
    assert not (media_root / "demo.srt").exists()


def test_source_change_during_translation_rejects_publication(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "1a" * 31, "demo.mp4")
    source_path = job_dir / "source.subtitle.json"

    class ChangingClient(FakeClientFactory):
        async def translate_batch(self, segments, source_language, target_language, glossary):
            items = await super().translate_batch(
                segments, source_language, target_language, glossary
            )
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["segments"][0]["text"] = "changed concurrently"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            return items

    client = ChangingClient()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run()

    assert summary.failed == 1
    assert not (job_dir / "zh-CN.subtitle.json").exists()
    assert not (media_root / "demo.srt").exists()


def test_srt_name_collision_is_reported_before_llm(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "5" * 62, "demo.mp4")
    create_job(data_root, media_root, "cd" + "6" * 62, "demo.mkv")
    client = FakeClientFactory()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run()

    assert summary.discovered == 2
    assert summary.failed == 2
    assert client.calls == 0
    assert not (media_root / "demo.srt").exists()


def test_unmanaged_or_user_modified_srt_is_never_overwritten(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "7" * 62, "demo.mp4")
    srt_path = media_root / "demo.srt"
    srt_path.write_text("user subtitle", encoding="utf-8")
    client = FakeClientFactory()
    settings = make_settings(data_root, media_root)

    first = BatchTranslator(settings, client).run()
    assert first.failed == 1
    assert client.calls == 0
    assert srt_path.read_text(encoding="utf-8") == "user subtitle"

    srt_path.unlink()
    assert BatchTranslator(settings, client).run().completed == 1
    srt_path.write_text("user edited subtitle", encoding="utf-8")
    second = BatchTranslator(settings, client).run()
    assert second.failed == 1
    assert client.calls == 1
    assert srt_path.read_text(encoding="utf-8") == "user edited subtitle"


def test_active_lock_marks_job_busy_and_stale_lock_is_recovered(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_id = "ab" + "8" * 62
    create_job(data_root, media_root, job_id, "demo.mp4")
    settings = make_settings(data_root, media_root, stale_lock_seconds=1)
    lock_path = data_root / "locks" / f"{job_id}.translate.zh-CN.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{}", encoding="utf-8")
    client = FakeClientFactory()

    assert BatchTranslator(settings, client).run().busy == 1
    assert client.calls == 0

    old = time.time() - 10
    os.utime(lock_path, (old, old))
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.calls == 1
    assert not lock_path.exists()


def test_invalid_input_does_not_call_llm_and_later_job_continues(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    bad_dir = create_job(data_root, media_root, "ab" + "9" * 62, "bad.mp4")
    create_job(data_root, media_root, "cd" + "0" * 62, "good.mp4")
    source_path = bad_dir / "source.subtitle.json"
    value = json.loads(source_path.read_text(encoding="utf-8"))
    value["segments"].append(value["segments"][0])
    source_path.write_text(json.dumps(value), encoding="utf-8")
    client = FakeClientFactory()

    summary = BatchTranslator(make_settings(data_root, media_root), client).run()

    assert summary.failed == 1
    assert summary.completed == 1
    assert client.calls == 1
    failed_state = TranslationState.model_validate_json(
        (bad_dir / "translate.zh-CN.state.json").read_text(encoding="utf-8")
    )
    assert failed_state.error is not None and failed_state.error.stage == "input"


def test_one_translation_failure_does_not_stop_later_jobs(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "a" * 62, "bad.mp4", text="FAIL")
    create_job(data_root, media_root, "cd" + "b" * 62, "good.mp4")
    client = FakeClientFactory(fail_text="FAIL")

    summary = BatchTranslator(make_settings(data_root, media_root), client).run()

    assert summary.failed == 1
    assert summary.completed == 1
    assert client.calls == 2


def test_max_attempts_prevents_additional_llm_calls(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    create_job(data_root, media_root, "ab" + "f" * 62, "demo.mp4", text="FAIL")
    client = FakeClientFactory(fail_text="FAIL")
    settings = make_settings(data_root, media_root, max_attempts=2)

    assert BatchTranslator(settings, client).run().failed == 1
    assert BatchTranslator(settings, client).run().failed == 1
    assert BatchTranslator(settings, client).run().failed == 1
    assert client.calls == 2


def test_batch_progress_resumes_without_repeating_completed_paid_batch(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    media_root = tmp_path / "media"
    job_id = "ab" + "12" * 31
    job_dir = create_job(data_root, media_root, job_id, "demo.mp4")
    source_path = job_dir / "source.subtitle.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["segments"] = [
        {"id": 0, "start": 0, "end": 1, "text": "first"},
        {"id": 1, "start": 1, "end": 2, "text": "second"},
    ]
    source_path.write_text(json.dumps(source), encoding="utf-8")

    class InterruptOnceClient(FakeClientFactory):
        def __init__(self) -> None:
            super().__init__()
            self.requested_ids = []
            self.interrupted = False

        async def translate_batch(self, segments, source_language, target_language, glossary):
            self.calls += 1
            ids = [segment.id for segment in segments]
            self.requested_ids.append(ids)
            if ids == [1] and not self.interrupted:
                self.interrupted = True
                raise RuntimeError("temporary interruption")
            return [
                TranslatedItem(id=segment.id, text=f"中文：{segment.text}") for segment in segments
            ]

    client = InterruptOnceClient()
    settings = make_settings(data_root, media_root, batch_size=1)

    assert BatchTranslator(settings, client).run().failed == 1
    progress_path = job_dir / "translate.zh-CN.progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["completed_ids"] == [0]

    assert BatchTranslator(settings, client).run().completed == 1
    assert client.requested_ids == [[0], [1], [1]]
    assert not progress_path.exists()


def test_api_key_is_not_part_of_translation_profile(tmp_path: Path) -> None:
    from subtitle_translator.batch import build_profile
    from subtitle_translator.glossary import GlossaryDocument, GlossaryTerm

    first = make_settings(tmp_path / "data", tmp_path / "media", llm_api_key="secret-one")
    second = make_settings(tmp_path / "data", tmp_path / "media", llm_api_key="secret-two")

    assert (
        build_profile(first, GlossaryDocument()).fingerprint
        == build_profile(second, GlossaryDocument()).fingerprint
    )

    required = GlossaryDocument(
        terms=[GlossaryTerm(source="base", target="地台", enforcement="required")]
    )
    preferred = GlossaryDocument(
        terms=[GlossaryTerm(source="base", target="地台", enforcement="preferred")]
    )
    assert build_profile(first, required).fingerprint == build_profile(first, preferred).fingerprint


class PolicyClient(FakeClientFactory):
    def __init__(self):
        super().__init__()
        self.translated_ids = []
        self.repaired_ids = []
        self.fail_repair = False
        self.fail_second = False

    async def translate_batch(self, segments, source_language, target_language, glossary):
        self.translated_ids.append([item.id for item in segments])
        if self.fail_second and any(item.id == 1 for item in segments):
            raise RuntimeError("interrupted second batch")
        return [
            TranslatedItem(id=item.id, text="给轮子上色" if item.id == 0 else "保留这句")
            for item in segments
        ]

    async def repair_batch(
        self,
        segments,
        source_language,
        target_language,
        glossary,
        *,
        previous_translations=(),
        failure_reasons=None,
    ):
        self.repaired_ids.append([item.id for item in segments])
        if self.fail_repair:
            raise RuntimeError("repair unavailable")
        return [TranslatedItem(id=item.id, text="给负重轮上色") for item in segments]


def write_policy(path, enforcement, accepted_targets=(), **overrides):
    term = {
        "source": "road wheel",
        "target": "负重轮",
        "enforcement": enforcement,
        "accepted_targets": list(accepted_targets),
    }
    term.update(overrides)
    path.write_text(json.dumps({"version": 1, "terms": [term]}), encoding="utf-8")


def policy_job(tmp_path, **overrides):
    data_root, media_root = tmp_path / "data", tmp_path / "media"
    job_dir = create_job(data_root, media_root, "ab" + "45" * 31, "demo.mp4")
    source_path = job_dir / "source.subtitle.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["segments"] = [
        {"id": 0, "start": 0, "end": 1, "text": "Paint the road wheel."},
        {"id": 1, "start": 1, "end": 2, "text": "Keep this sentence."},
    ]
    source_path.write_text(json.dumps(source), encoding="utf-8")
    glossary_path = tmp_path / "glossary.json"
    write_policy(glossary_path, "preferred")
    settings = make_settings(data_root, media_root, glossary_path=glossary_path, **overrides)
    return settings, job_dir, PolicyClient()


def test_tightening_policy_repairs_only_noncompliant_cached_segments(tmp_path):
    settings, job_dir, client = policy_job(tmp_path)
    assert BatchTranslator(settings, client).run().completed == 1
    old_state = json.loads((job_dir / "translate.zh-CN.state.json").read_text())
    write_policy(settings.glossary_path, "required")
    # Automatic single-job selection must include completed jobs needing revalidation.
    assert BatchTranslator(settings, client).run_one().completed == 1
    assert client.translated_ids == [[0, 1]]
    assert client.repaired_ids == [[0]]
    output = json.loads((job_dir / "zh-CN.subtitle.json").read_text())
    assert [item["text"] for item in output["segments"]] == ["给负重轮上色", "保留这句"]
    new_state = json.loads((job_dir / "translate.zh-CN.state.json").read_text())
    assert old_state["profile"] == new_state["profile"]
    assert old_state["validation_fingerprint"] != new_state["validation_fingerprint"]
    assert BatchTranslator(settings, client).run().skipped == 1


@pytest.mark.parametrize("change", ["relax", "allow_variant", "missing_fingerprint"])
def test_revalidation_of_compliant_output_needs_no_llm_or_republication(tmp_path, change):
    settings, job_dir, client = policy_job(tmp_path)
    write_policy(settings.glossary_path, "required", accepted_targets=["轮子"])
    assert BatchTranslator(settings, client).run().completed == 1
    output_path = job_dir / "zh-CN.subtitle.json"
    srt_path = settings.media_root / "demo.srt"
    output_before, srt_before = output_path.stat().st_mtime_ns, srt_path.stat().st_mtime_ns
    state_path = job_dir / "translate.zh-CN.state.json"
    if change == "relax":
        write_policy(settings.glossary_path, "preferred")
    elif change == "allow_variant":
        write_policy(settings.glossary_path, "required", accepted_targets=["轮子", "车轮"])
    else:
        state = json.loads(state_path.read_text())
        state.pop("validation_fingerprint")
        state_path.write_text(json.dumps(state))
    translator = BatchTranslator(settings, client)
    assert translator.run().skipped == 1
    assert client.translated_ids == [[0, 1]]
    assert client.repaired_ids == []
    assert output_path.stat().st_mtime_ns == output_before
    assert srt_path.stat().st_mtime_ns == srt_before
    assert json.loads(state_path.read_text())["validation_fingerprint"] == (
        translator.glossary.validation_fingerprint
    )


def test_removing_accepted_variant_repairs_existing_translation(tmp_path):
    settings, _, client = policy_job(tmp_path)
    write_policy(settings.glossary_path, "required", accepted_targets=["轮子"])
    assert BatchTranslator(settings, client).run().completed == 1
    write_policy(settings.glossary_path, "required")
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0, 1]]
    assert client.repaired_ids == [[0]]


def test_failed_repair_preserves_old_files_and_relaxing_policy_resets_attempts(tmp_path):
    settings, job_dir, client = policy_job(tmp_path, max_attempts=1)
    assert BatchTranslator(settings, client).run().completed == 1
    paths = [job_dir / "zh-CN.subtitle.json", settings.media_root / "demo.srt"]
    before = [path.read_bytes() for path in paths]
    write_policy(settings.glossary_path, "required")
    client.fail_repair = True
    assert BatchTranslator(settings, client).run().failed == 1
    assert [path.read_bytes() for path in paths] == before
    state = json.loads((job_dir / "translate.zh-CN.state.json").read_text())
    assert state["status"] == "failed"
    assert state["attempt"] == 1
    assert BatchTranslator(settings, client).run().failed == 1
    assert client.repaired_ids == [[0]]
    write_policy(settings.glossary_path, "preferred")
    assert BatchTranslator(settings, client).run_one().completed == 1
    assert client.translated_ids == [[0, 1]]
    assert client.repaired_ids == [[0]]


def test_changed_policy_revalidates_saved_prefix_before_resuming(tmp_path):
    settings, job_dir, client = policy_job(tmp_path, batch_size=1)
    client.fail_second = True
    assert BatchTranslator(settings, client).run().failed == 1
    progress_path = job_dir / "translate.zh-CN.progress.json"
    assert json.loads(progress_path.read_text())["completed_ids"] == [0]
    write_policy(settings.glossary_path, "required")
    client.fail_second = False
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0], [1], [1]]
    assert client.repaired_ids == [[0]]
    assert not progress_path.exists()


def test_export_retry_revalidates_glossary_before_publishing(tmp_path, monkeypatch):
    from subtitle_translator import batch

    settings, _, client = policy_job(tmp_path)
    publish = batch.publish_srt

    def fail_publish(*args):
        raise OSError("export unavailable")

    monkeypatch.setattr(batch, "publish_srt", fail_publish)
    assert BatchTranslator(settings, client).run().failed == 1
    write_policy(settings.glossary_path, "required")
    monkeypatch.setattr(batch, "publish_srt", publish)
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0, 1]]
    assert client.repaired_ids == [[0]]
    assert "负重轮" in (settings.media_root / "demo.srt").read_text()


def test_tightening_policy_preserves_user_modified_srt_before_llm(tmp_path):
    settings, job_dir, client = policy_job(tmp_path)
    assert BatchTranslator(settings, client).run().completed == 1
    before = (job_dir / "zh-CN.subtitle.json").read_bytes()
    srt_path = settings.media_root / "demo.srt"
    srt_path.write_text("user edits", encoding="utf-8")
    write_policy(settings.glossary_path, "required")
    assert BatchTranslator(settings, client).run().failed == 1
    assert not client.repaired_ids
    assert srt_path.read_text() == "user edits"
    assert (job_dir / "zh-CN.subtitle.json").read_bytes() == before


@pytest.mark.parametrize(
    "change", [{"usage": "车辆行走机构"}, {"aliases": ["road wheels"]}, {"target": "承重轮"}]
)
def test_translation_affecting_glossary_changes_invalidate_cache(tmp_path, change):
    settings, _, client = policy_job(tmp_path)
    assert BatchTranslator(settings, client).run().completed == 1
    write_policy(settings.glossary_path, "preferred", **change)
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0, 1], [0, 1]]


def test_interrupted_cached_repairs_resume_without_repeating_successful_repair(tmp_path):
    settings, job_dir, client = policy_job(tmp_path, batch_size=1)
    source_path = job_dir / "source.subtitle.json"
    source = json.loads(source_path.read_text())
    source["segments"][1]["text"] = "Paint another road wheel."
    source_path.write_text(json.dumps(source))
    assert BatchTranslator(settings, client).run().completed == 1
    output_path = job_dir / "zh-CN.subtitle.json"
    srt_path = settings.media_root / "demo.srt"
    old_output, old_srt = output_path.read_bytes(), srt_path.read_bytes()
    write_policy(settings.glossary_path, "required")
    original_repair = client.repair_batch
    interrupted = False

    async def interrupt_second(segments, *args, **kwargs):
        nonlocal interrupted
        if segments[0].id == 1 and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted repair")
        return await original_repair(segments, *args, **kwargs)

    client.repair_batch = interrupt_second
    assert BatchTranslator(settings, client).run().failed == 1
    assert output_path.read_bytes() == old_output
    assert srt_path.read_bytes() == old_srt
    saved = json.loads((job_dir / "translate.zh-CN.progress.json").read_text())
    assert saved["translations"][0]["text"] == "给负重轮上色"
    assert saved["translations"][1]["text"] == "保留这句"
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0], [1]]
    assert client.repaired_ids == [[0], [1]]
    assert [item["text"] for item in json.loads(output_path.read_text())["segments"]] == [
        "给负重轮上色",
        "给负重轮上色",
    ]


def test_source_and_prompt_version_changes_do_not_reuse_prior_translation(tmp_path, monkeypatch):
    from subtitle_translator import batch

    settings, job_dir, client = policy_job(tmp_path)
    assert BatchTranslator(settings, client).run().completed == 1
    monkeypatch.setattr(batch, "PROMPT_VERSION", batch.PROMPT_VERSION + 1)
    assert BatchTranslator(settings, client).run().completed == 1
    source_path = job_dir / "source.subtitle.json"
    source = json.loads(source_path.read_text())
    source["segments"][1]["text"] = "A changed sentence."
    source_path.write_text(json.dumps(source))
    assert BatchTranslator(settings, client).run().completed == 1
    assert client.translated_ids == [[0, 1], [0, 1], [0, 1]]
