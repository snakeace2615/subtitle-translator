import json
from types import SimpleNamespace

from subtitle_translator.main import main
from subtitle_translator.mounting import MountError


def test_mount_failure_stops_before_uvicorn(monkeypatch, capsys) -> None:
    def failed_mount(settings):
        raise MountError("media unavailable")

    def unexpected_uvicorn(*args, **kwargs):
        raise AssertionError("uvicorn must not start after a mount failure")

    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", failed_mount)
    monkeypatch.setattr("subtitle_translator.main.uvicorn.run", unexpected_uvicorn)

    assert main([]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "media unavailable",
    }


def test_scan_prints_summary_and_returns_one_when_a_job_failed(monkeypatch, capsys) -> None:
    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", lambda settings: False)

    class FakeBatchTranslator:
        def __init__(self, settings):
            pass

        def run(self):
            return SimpleNamespace(
                failed=1,
                as_dict=lambda: {
                    "discovered": 2,
                    "completed": 1,
                    "skipped": 0,
                    "failed": 1,
                    "busy": 0,
                },
            )

    monkeypatch.setattr("subtitle_translator.main.BatchTranslator", FakeBatchTranslator)

    assert main(["scan"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "complete",
        "discovered": 2,
        "completed": 1,
        "skipped": 0,
        "failed": 1,
        "busy": 0,
    }


def test_translate_one_passes_exact_media_path_to_batch_translator(monkeypatch, capsys) -> None:
    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", lambda settings: False)
    selected_paths = []

    class FakeBatchTranslator:
        def __init__(self, settings):
            pass

        def run(self):
            raise AssertionError("full scan must not run")

        def run_one(self, media_relative_path):
            selected_paths.append(media_relative_path)
            return SimpleNamespace(
                failed=0,
                as_dict=lambda: {
                    "discovered": 1,
                    "completed": 1,
                    "skipped": 0,
                    "failed": 0,
                    "busy": 0,
                },
            )

    monkeypatch.setattr("subtitle_translator.main.BatchTranslator", FakeBatchTranslator)

    assert main(["translate-one", "课程/demo.mp4"]) == 0
    assert selected_paths == ["课程/demo.mp4"]
    assert json.loads(capsys.readouterr().out)["completed"] == 1


def test_translate_one_allows_omitting_media_path(monkeypatch, capsys) -> None:
    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", lambda settings: False)
    selected_paths = []

    class FakeBatchTranslator:
        def __init__(self, settings):
            pass

        def run_one(self, media_relative_path):
            selected_paths.append(media_relative_path)
            return SimpleNamespace(
                failed=0,
                as_dict=lambda: {
                    "discovered": 1,
                    "completed": 1,
                    "skipped": 0,
                    "failed": 0,
                    "busy": 0,
                },
            )

    monkeypatch.setattr("subtitle_translator.main.BatchTranslator", FakeBatchTranslator)

    assert main(["translate-one"]) == 0
    assert selected_paths == [None]
    assert json.loads(capsys.readouterr().out)["completed"] == 1
