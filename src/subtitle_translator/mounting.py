from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from subtitle_translator.config import Settings

LOGGER = logging.getLogger(__name__)


class MountError(RuntimeError):
    """Raised when the configured media root is not safely writable."""


def ensure_media_mount(settings: Settings) -> bool:
    """Mount and validate the configured media root before startup.

    Returns True when this call mounted the source and False when automatic
    mounting was disabled or the target was already mounted.
    """
    source = (settings.media_mount_source or "").strip()
    target = settings.media_root.expanduser().resolve()

    if target.exists() and not target.is_dir():
        raise MountError(f"Media mount target is not a directory: {target}")

    mounted = False
    if source:
        if os.path.ismount(target):
            LOGGER.info("Media directory is already mounted: %s", target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            command = _mount_command(settings, source, target)
            LOGGER.info("Mounting media source %s at %s", source, target)
            try:
                result = subprocess.run(command, check=False)
            except OSError as exc:
                raise MountError(f"Could not start mount command: {exc}") from exc

            if result.returncode != 0:
                raise MountError(
                    f"Could not mount media source {source!r} at {target} "
                    f"(mount exited with status {result.returncode})"
                )
            if not os.path.ismount(target):
                raise MountError(f"Mount command completed but {target} is not a mount point")
            mounted = True
            LOGGER.info("Mounted media source %s at %s", source, target)
    elif not target.is_dir():
        raise MountError(f"Media root not found: {target}")

    _verify_writable(target)
    return mounted


def _verify_writable(target: Path) -> None:
    try:
        with tempfile.NamedTemporaryFile(
            dir=target,
            prefix=".subtitle-translator-write-test-",
        ):
            pass
    except OSError as exc:
        raise MountError(f"Media root is not writable: {target}: {exc}") from exc


def _mount_command(settings: Settings, source: str, target: Path) -> list[str]:
    command = [] if os.geteuid() == 0 else ["sudo"]
    command.extend(["mount", "-t", settings.media_mount_type])
    options = settings.media_mount_options.strip()
    if options:
        command.extend(["-o", options])
    command.extend([source, str(target)])
    return command
