from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from subtitle_translator.models import TranslatedItem

STATE_VERSION = 1


class ExtractionSource(BaseModel):
    relative_path: str = Field(min_length=1)


class ExtractionState(BaseModel):
    version: Literal[1] = 1
    job_id: str = Field(min_length=1)
    source: ExtractionSource
    status: Literal["pending", "processing", "complete", "failed"]
    output: str = "source.subtitle.json"


class ProfileReference(BaseModel):
    fingerprint: str
    version: int = Field(ge=1)
    model: str


class ExportReference(BaseModel):
    relative_path: str
    format: Literal["srt"] = "srt"
    layout_fingerprint: str | None = None
    cue_count: int | None = None
    sha256: str | None = None
    previous_sha256: str | None = None
    status: Literal["pending", "complete", "failed"]


class StateError(BaseModel):
    stage: Literal["input", "translation", "export"]
    type: str
    message: str


class TranslationState(BaseModel):
    version: Literal[1] = STATE_VERSION
    job_id: str
    target_language: str
    source_sha256: str
    profile: ProfileReference
    presentation_fingerprint: str | None = None
    validation_fingerprint: str | None = None
    status: Literal["pending", "processing", "complete", "failed"]
    attempt: int = Field(ge=1)
    output: str
    output_sha256: str | None = None
    export: ExportReference | None = None
    started_at: str
    completed_at: str | None = None
    error: StateError | None = None


class TranslationProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    job_id: str
    target_language: str
    source_sha256: str
    profile_fingerprint: str
    validation_fingerprint: str | None = None
    batch_size: int = Field(ge=1)
    completed_ids: list[int]
    translations: list[TranslatedItem]
    updated_at: str

    @model_validator(mode="after")
    def completed_ids_match_translations(self) -> TranslationProgress:
        if self.completed_ids != [item.id for item in self.translations]:
            raise ValueError("completed_ids do not match saved translations")
        if len(set(self.completed_ids)) != len(self.completed_ids):
            raise ValueError("saved translation IDs must be unique")
        return self


class TaskLock:
    def __init__(self, path: Path, stale_seconds: int) -> None:
        self.path = path
        self.stale_seconds = stale_seconds
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                )
            except FileExistsError:
                if not self._is_stale():
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue

            payload = self._payload()
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return True

    def is_active(self) -> bool:
        """Return whether an existing lock currently prevents acquisition."""
        return self.path.exists() and not self._is_stale()

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") != self.token:
                return
            payload["heartbeat_at"] = utc_now()
            with self.path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, json.JSONDecodeError):
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            self.acquired = False

    def _payload(self) -> dict[str, str | int]:
        timestamp = utc_now()
        return {
            "token": self.token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": timestamp,
            "heartbeat_at": timestamp,
        }

    def _is_stale(self) -> bool:
        try:
            age_seconds = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age_seconds <= self.stale_seconds:
            return False

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True

        if payload.get("host") != socket.gethostname():
            return True
        pid = payload.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


def atomic_write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(model.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        type(model).model_validate_json(temporary_path.read_text(encoding="utf-8"))
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
