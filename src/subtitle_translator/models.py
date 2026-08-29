from pydantic import BaseModel, Field, model_validator


class SubtitleSegment(BaseModel):
    id: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def end_must_follow_start(self) -> "SubtitleSegment":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class SubtitleDocument(BaseModel):
    schema_version: str = "subtitle-document/v1"
    media_file: str
    source_language: str
    target_language: str | None = None
    segments: list[SubtitleSegment]


class TranslationRequest(BaseModel):
    document: SubtitleDocument
    target_language: str = "zh-CN"
    glossary: dict[str, str] = Field(default_factory=dict)


class TranslationResponse(BaseModel):
    model: str
    document: SubtitleDocument


class TranslatedItem(BaseModel):
    id: int
    text: str = Field(min_length=1)

