from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from subtitle_translator.glossary import GlossaryDocument


class SubtitleSegment(BaseModel):
    id: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "SubtitleSegment":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class SubtitleDocument(BaseModel):
    schema_version: Literal["subtitle-document/v1"] = "subtitle-document/v1"
    media_file: str = Field(min_length=1)
    source_language: str = Field(min_length=1)
    target_language: str | None = None
    segments: list[SubtitleSegment]

    @field_validator("media_file", "source_language")
    @classmethod
    def string_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def segments_must_be_unique_and_ordered(self) -> "SubtitleDocument":
        seen_ids: set[int] = set()
        previous_start = -1.0
        for segment in self.segments:
            if segment.id in seen_ids:
                raise ValueError(f"duplicate segment id: {segment.id}")
            if segment.start < previous_start:
                raise ValueError("segments must be ordered by start time")
            seen_ids.add(segment.id)
            previous_start = segment.start
        return self


class TranslationRequest(BaseModel):
    document: SubtitleDocument
    target_language: str = "zh-CN"
    glossary: GlossaryDocument | dict[str, str] = Field(default_factory=dict)


class TranslationResponse(BaseModel):
    model: str
    document: SubtitleDocument


class TranslatedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("translated text must not be blank")
        return value
