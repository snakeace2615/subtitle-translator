from pathlib import Path

import pytest

from subtitle_translator.srt import publish_srt
from subtitle_translator.state import ProfileReference, TranslationState, atomic_write_model


def make_state(status: str) -> TranslationState:
    return TranslationState(
        job_id="ab123",
        target_language="zh-CN",
        source_sha256="sha256:source",
        profile=ProfileReference(
            fingerprint="sha256:profile",
            version=1,
            model="model",
        ),
        status=status,
        attempt=1,
        output="zh-CN.subtitle.json",
        started_at="2026-01-01T00:00:00+00:00",
    )


def test_interrupted_state_replace_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    atomic_write_model(path, make_state("processing"))
    original = path.read_bytes()

    def interrupted_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr("subtitle_translator.state.os.replace", interrupted_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        atomic_write_model(path, make_state("complete"))

    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_interrupted_srt_replace_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "demo.srt"
    path.write_text("existing", encoding="utf-8")

    def interrupted_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr("subtitle_translator.srt.os.replace", interrupted_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        publish_srt(path, "1\n00:00:00,000 --> 00:00:01,000\n字幕\n", 1)

    assert path.read_text(encoding="utf-8") == "existing"
    assert not list(tmp_path.glob("*.tmp"))
