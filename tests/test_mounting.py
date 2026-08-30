from pathlib import Path
from types import SimpleNamespace

import pytest

from subtitle_translator.config import Settings
from subtitle_translator.mounting import MountError, ensure_media_mount


def make_settings(target: Path, **overrides) -> Settings:
    values = {
        "media_root": target,
        "media_mount_source": r"Y:\media",
        "media_mount_type": "drvfs",
        "media_mount_options": "rw",
    }
    values.update(overrides)
    return Settings(**values)


def test_already_mounted_writable_media_root_is_left_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("subtitle_translator.mounting.os.path.ismount", lambda path: True)

    def unexpected_run(*args, **kwargs):
        raise AssertionError("mount command should not run")

    monkeypatch.setattr("subtitle_translator.mounting.subprocess.run", unexpected_run)

    assert ensure_media_mount(make_settings(tmp_path)) is False


def test_unmounted_media_root_is_mounted_rw_before_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "media"
    mount_checks = iter([False, True])
    commands = []
    monkeypatch.setattr(
        "subtitle_translator.mounting.os.path.ismount", lambda path: next(mount_checks)
    )
    monkeypatch.setattr("subtitle_translator.mounting.os.geteuid", lambda: 1000)

    def successful_run(command, check):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subtitle_translator.mounting.subprocess.run", successful_run)

    assert ensure_media_mount(make_settings(target)) is True
    assert commands == [["sudo", "mount", "-t", "drvfs", "-o", "rw", r"Y:\media", str(target)]]


def test_failed_mount_stops_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "media"
    monkeypatch.setattr("subtitle_translator.mounting.os.path.ismount", lambda path: False)
    monkeypatch.setattr("subtitle_translator.mounting.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "subtitle_translator.mounting.subprocess.run",
        lambda command, check: SimpleNamespace(returncode=32),
    )

    with pytest.raises(MountError, match="mount exited with status 32"):
        ensure_media_mount(make_settings(target))


def test_read_only_media_root_stops_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("subtitle_translator.mounting.os.path.ismount", lambda path: True)

    def denied(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr("subtitle_translator.mounting.tempfile.NamedTemporaryFile", denied)

    with pytest.raises(MountError, match="Media root is not writable"):
        ensure_media_mount(make_settings(tmp_path))


def test_empty_mount_source_uses_existing_writable_directory(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, media_mount_source="")

    assert ensure_media_mount(settings) is False
