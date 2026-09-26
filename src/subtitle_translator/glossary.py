from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class GlossaryTerm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    case_sensitive: bool = False
    enforcement: Literal["required", "preferred"] = "required"
    accepted_targets: list[str] = Field(default_factory=list)
    usage: str | None = None

    @field_validator("source", "target", "usage")
    @classmethod
    def non_blank_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("glossary values must not be blank")
        return value

    @field_validator("aliases", "accepted_targets")
    @classmethod
    def non_blank_aliases(cls, aliases: list[str]) -> list[str]:
        normalized = [alias.strip() for alias in aliases]
        if any(not alias for alias in normalized):
            raise ValueError("glossary variants must not be blank")
        return normalized

    def variants(self) -> tuple[str, ...]:
        return (self.source, *self.aliases)

    def accepts(self, text: str) -> bool:
        return any(target in text for target in (self.target, *self.accepted_targets))


class GlossaryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    terms: list[GlossaryTerm] = Field(default_factory=list)

    @model_validator(mode="after")
    def terms_must_not_conflict(self) -> GlossaryDocument:
        variants: list[tuple[str, bool, str]] = []
        for term in self.terms:
            local: list[str] = []
            for variant in term.variants():
                if any(
                    _variants_conflict(variant, term.case_sensitive, item, term.case_sensitive)
                    for item in local
                ):
                    raise ValueError(f"duplicate source or alias in glossary term: {variant}")
                local.append(variant)
                for existing, existing_case_sensitive, existing_target in variants:
                    if _variants_conflict(
                        variant,
                        term.case_sensitive,
                        existing,
                        existing_case_sensitive,
                    ):
                        detail = (
                            "multiple targets" if existing_target != term.target else "duplicate"
                        )
                        raise ValueError(f"glossary {detail} source or alias: {variant}")
                variants.append((variant, term.case_sensitive, term.target))
        return self

    @classmethod
    def from_value(cls, value: Any) -> GlossaryDocument:
        if isinstance(value, GlossaryDocument):
            return value
        if isinstance(value, Mapping) and "version" not in value and "terms" not in value:
            if not all(
                isinstance(key, str) and isinstance(target, str) for key, target in value.items()
            ):
                raise ValueError("legacy glossary must contain string sources and targets")
            return cls(
                terms=[
                    GlossaryTerm(source=source, target=target) for source, target in value.items()
                ]
            )
        return cls.model_validate(value)

    def canonical_json(self) -> str:
        return self._canonical_json(include_validation=True)

    def prompt_canonical_json(self) -> str:
        """Return all glossary fields supplied to the model."""
        return self.canonical_json()

    def translation_canonical_json(self) -> str:
        """Reuse translations after revalidation when only acceptance rules change.

        Enforcement and accepted targets DO affect prompts, but do not by themselves
        require regeneration of translations that already satisfy the new rules.
        """
        return self._canonical_json(include_validation=False)

    def _canonical_json(self, *, include_validation: bool) -> str:
        value = self.model_dump(mode="json")
        for term in value["terms"]:
            if not include_validation:
                term.pop("enforcement")
                term.pop("accepted_targets")
            else:
                term["accepted_targets"] = sorted(set(term["accepted_targets"]))
            term["aliases"] = sorted(term["aliases"], key=lambda alias: (alias.casefold(), alias))
        value["terms"] = sorted(
            value["terms"],
            key=lambda term: (
                term["source"].casefold(),
                term["source"],
                term["target"],
                term["case_sensitive"],
                term["aliases"],
            ),
        )
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @property
    def validation_fingerprint(self) -> str:
        content = "glossary-validation/v2:" + self.canonical_json()
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _variants_conflict(
    left: str,
    left_case_sensitive: bool,
    right: str,
    right_case_sensitive: bool,
) -> bool:
    left = _normalize_matching_text(left)
    right = _normalize_matching_text(right)
    if left == right:
        return True
    return (
        not (left_case_sensitive and right_case_sensitive) and left.casefold() == right.casefold()
    )


def load_glossary(path: Path) -> GlossaryDocument:
    resolved = path.expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = " ".join(str(exc).split())
        raise ValueError(
            f"Could not load glossary {resolved}: {type(exc).__name__}: {message}"
        ) from exc
    try:
        return GlossaryDocument.from_value(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid glossary {resolved}: {exc}") from exc


def atomic_write_glossary(path: Path, glossary: GlossaryDocument) -> None:
    validated = GlossaryDocument.model_validate(glossary.model_dump())
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(validated.model_dump_json(indent=2))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        load_glossary(temporary_path)
        os.replace(temporary_path, resolved)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def set_term(
    glossary: GlossaryDocument,
    source: str,
    target: str,
    enforcement: Literal["required", "preferred"] | None = None,
) -> GlossaryDocument:
    source = source.strip()
    target = target.strip()
    terms = list(glossary.terms)
    for index, term in enumerate(terms):
        matches = source == term.source or (
            not term.case_sensitive and source.casefold() == term.source.casefold()
        )
        if matches:
            updated = term.model_dump()
            updated["target"] = target
            if enforcement is not None:
                updated["enforcement"] = enforcement
            terms[index] = GlossaryTerm.model_validate(updated)
            return GlossaryDocument(terms=terms)
    terms.append(
        GlossaryTerm(
            source=source,
            target=target,
            enforcement=enforcement or "required",
        )
    )
    return GlossaryDocument(terms=terms)


def remove_term(glossary: GlossaryDocument, source: str) -> GlossaryDocument:
    source = source.strip()
    retained = [
        term
        for term in glossary.terms
        if not (
            source == term.source
            or (not term.case_sensitive and source.casefold() == term.source.casefold())
        )
    ]
    if len(retained) == len(glossary.terms):
        raise ValueError(f"Glossary source not found: {source}")
    return GlossaryDocument(terms=retained)


def matched_terms(texts: Sequence[str], glossary: GlossaryDocument) -> list[GlossaryTerm]:
    matched_indexes: set[int] = set()
    for text in texts:
        matched_indexes.update(_matched_indexes(text, glossary))
    return [term for index, term in enumerate(glossary.terms) if index in matched_indexes]


def _matched_indexes(text: str, glossary: GlossaryDocument) -> set[int]:
    matches: list[tuple[int, int, int, GlossaryTerm]] = []
    text = _normalize_matching_text(text)
    for term_index, term in enumerate(glossary.terms):
        for variant in term.variants():
            for start, end in _find_variant(text, variant, term.case_sensitive):
                matches.append((start, end, term_index, term))

    selected: list[tuple[int, int, int, GlossaryTerm]] = []
    for candidate in sorted(matches, key=lambda item: (-(item[1] - item[0]), item[0], item[2])):
        if any(candidate[0] < existing[1] and existing[0] < candidate[1] for existing in selected):
            continue
        selected.append(candidate)

    return {item[2] for item in selected}


def term_is_present(text: str, term: GlossaryTerm) -> bool:
    return any(_find_variant(text, variant, term.case_sensitive) for variant in term.variants())


def _find_variant(text: str, variant: str, case_sensitive: bool) -> list[tuple[int, int]]:
    text = _normalize_matching_text(text)
    variant = _normalize_matching_text(variant)
    flags = 0 if case_sensitive else re.IGNORECASE
    escaped = re.escape(variant)
    if any(character.isascii() and character.isalnum() for character in variant):
        escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return [(match.start(), match.end()) for match in re.finditer(escaped, text, flags)]


def _normalize_matching_text(text: str) -> str:
    # Normalize word separators without stemming or fuzzy matching.
    return re.sub(r"[\s\-\u2010\u2011]+", " ", text).strip()
