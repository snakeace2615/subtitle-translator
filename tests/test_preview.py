import json
from pathlib import Path

import pytest

from subtitle_translator.config import Settings
from subtitle_translator.main import main
from subtitle_translator.preview import create_preview


def prepare(tmp_path: Path) -> tuple[Path, Settings]:
    job = tmp_path / "data" / "job"
    job.mkdir(parents=True)
    value = {
        "media_file": "demo.mp4",
        "source_language": "en",
        "segments": [{"id": 0, "start": 0, "end": 30, "text": "Thank you."}],
    }
    (job / "source.subtitle.json").write_text(json.dumps(value))
    value.update(target_language="zh-CN")
    value["segments"][0]["text"] = "谢谢。"
    (job / "zh-CN.subtitle.json").write_text(json.dumps(value))
    return job, Settings(_env_file=None, data_dir=tmp_path / "data", media_root=tmp_path / "media")


def test_preview_needs_no_mount_or_model_and_preserves_job(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    job, settings = prepare(tmp_path)
    before = {path: path.read_bytes() for path in job.iterdir()}
    monkeypatch.setattr("subtitle_translator.main.get_settings", lambda: settings)

    def unexpected(*args):
        raise AssertionError("preview must not mount media or create LLM")

    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", unexpected)
    monkeypatch.setattr("subtitle_translator.service.DeepSeekClient", unexpected)
    out = tmp_path / "preview"
    assert main(["preview", str(job), "--output-dir", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == 1
    assert (out / "preview.srt").is_file()
    assert json.loads((out / "quality.json").read_text())["issues"][0]["code"] == "long_duration"
    assert {path: path.read_bytes() for path in job.iterdir()} == before
    with pytest.raises(FileExistsError):
        create_preview(job, out, settings)


def test_preview_rejects_output_in_production_roots(tmp_path: Path) -> None:
    job, settings = prepare(tmp_path)
    for path in (job / "preview", settings.media_root / "preview", settings.data_dir / "preview"):
        with pytest.raises(ValueError, match="outside"):
            create_preview(job, path, settings)
        assert not path.exists()
