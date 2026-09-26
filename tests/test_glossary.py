import json
from pathlib import Path

import pytest

from subtitle_translator.config import Settings
from subtitle_translator.glossary import (
    GlossaryDocument,
    GlossaryTerm,
    atomic_write_glossary,
    load_glossary,
    matched_terms,
    remove_term,
    set_term,
)
from subtitle_translator.main import main


def test_legacy_glossary_loads_and_atomic_write_migrates_it(tmp_path: Path) -> None:
    path = tmp_path / "glossary.json"
    path.write_text('{"weathering":"旧化","airbrush":"喷笔"}', encoding="utf-8")

    glossary = load_glossary(path)
    atomic_write_glossary(path, glossary)

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["version"] == 1
    assert [term["source"] for term in migrated["terms"]] == ["weathering", "airbrush"]
    assert not list(tmp_path.glob(".glossary.json.*.tmp"))


@pytest.mark.parametrize(
    "terms",
    [
        [
            {"source": "weathering", "target": "旧化"},
            {"source": "Weathering", "target": "做旧"},
        ],
        [
            {"source": "airbrush", "target": "喷笔", "aliases": ["spray gun"]},
            {"source": "spray gun", "target": "喷枪"},
        ],
        [{"source": "airbrush", "target": "喷笔", "aliases": [""]}],
    ],
)
def test_glossary_rejects_duplicate_and_alias_conflicts(terms) -> None:
    with pytest.raises(ValueError):
        GlossaryDocument(terms=terms)


def test_matching_is_case_insensitive_bounded_and_prefers_longest_phrase() -> None:
    short = GlossaryTerm(source="weathering", target="旧化")
    long = GlossaryTerm(source="weathering effects", target="旧化效果")
    glossary = GlossaryDocument(terms=[short, long])

    assert matched_terms(["WEATHERING effects"], glossary) == [long]
    assert matched_terms(["preweathering"], glossary) == []
    assert matched_terms(["weathering and weathering effects"], glossary) == [short, long]


def test_set_remove_and_fingerprint_use_normalized_content() -> None:
    first = GlossaryDocument(
        terms=[
            GlossaryTerm(source="weathering", target="旧化"),
            GlossaryTerm(source="airbrush", target="喷笔"),
        ]
    )
    reordered = GlossaryDocument(terms=list(reversed(first.terms)))
    assert first.fingerprint == reordered.fingerprint

    changed = set_term(first, "WEATHERING", "做旧", enforcement="preferred")
    assert changed.terms[0].target == "做旧"
    assert changed.terms[0].enforcement == "preferred"
    assert remove_term(changed, "weathering").terms == [first.terms[1]]


def test_enforcement_changes_prompt_and_validation_but_allows_revalidated_reuse() -> None:
    required = GlossaryDocument(
        terms=[GlossaryTerm(source="base", target="地台", enforcement="required")]
    )
    preferred = GlossaryDocument(
        terms=[GlossaryTerm(source="base", target="地台", enforcement="preferred")]
    )

    assert required.fingerprint != preferred.fingerprint
    assert required.prompt_canonical_json() != preferred.prompt_canonical_json()
    assert required.validation_fingerprint != preferred.validation_fingerprint
    assert required.translation_canonical_json() == preferred.translation_canonical_json()


def test_set_term_preserves_existing_enforcement_when_level_is_omitted() -> None:
    glossary = GlossaryDocument(
        terms=[GlossaryTerm(source="base", target="地台", enforcement="preferred")]
    )

    changed = set_term(glossary, "BASE", "底座")

    assert changed.terms[0].target == "底座"
    assert changed.terms[0].enforcement == "preferred"


def test_glossary_cli_does_not_check_media_mount_or_model(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    glossary_path = tmp_path / "glossary.json"
    settings = Settings(_env_file=None, glossary_path=glossary_path, llm_api_key="")
    monkeypatch.setattr("subtitle_translator.main.get_settings", lambda: settings)

    def unexpected_mount(settings) -> None:
        raise AssertionError("glossary commands must not check the media mount")

    monkeypatch.setattr("subtitle_translator.main.ensure_media_mount", unexpected_mount)

    assert (
        main(
            [
                "glossary",
                "set",
                "weathering",
                "旧化",
                "--enforcement",
                "preferred",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert main(["glossary", "validate"]) == 0
    assert "fingerprint" in json.loads(capsys.readouterr().out)
    assert main(["glossary", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["glossary"]["terms"][0]
    assert listed["target"] == "旧化"
    assert listed["enforcement"] == "preferred"
    assert main(["glossary", "remove", "weathering"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


@pytest.mark.parametrize(
    "text", ["ROAD WHEELS", "road-wheels", "road\u2011wheels", "road   wheels"]
)
def test_plural_aliases_and_separators_match_longest_phrase(text) -> None:
    wheel = GlossaryTerm(source="Road wheel", target="负重轮", aliases=["Road wheels"])
    short = GlossaryTerm(source="road", target="道路", enforcement="preferred")
    assert matched_terms([text], GlossaryDocument(terms=[short, wheel])) == [wheel]


def test_matching_does_not_join_subtitle_boundaries_or_guess_word_forms() -> None:
    term = GlossaryTerm(source="road wheel", target="负重轮")
    glossary = GlossaryDocument(terms=[term])
    assert matched_terms(["road", "wheel"], glossary) == []
    assert matched_terms(["road wheels"], glossary) == []


def test_normalized_alias_conflicts_are_rejected() -> None:
    with pytest.raises(ValueError, match="multiple targets"):
        GlossaryDocument(
            terms=[
                GlossaryTerm(source="base coat", target="底色"),
                GlossaryTerm(source="base-coat", target="底漆"),
            ]
        )


@pytest.mark.parametrize("fields", [{"accepted_targets": [" "]}, {"usage": " "}])
def test_blank_translation_variants_and_usage_are_rejected(fields) -> None:
    with pytest.raises(ValueError):
        GlossaryTerm(source="road wheel", target="负重轮", **fields)


def test_accepted_targets_only_change_validation_and_prompt_fingerprints() -> None:
    original = GlossaryDocument(terms=[GlossaryTerm(source="weathering", target="旧化")])
    changed = GlossaryDocument(
        terms=[GlossaryTerm(source="weathering", target="旧化", accepted_targets=["做旧"])]
    )
    assert original.translation_canonical_json() == changed.translation_canonical_json()
    assert original.validation_fingerprint != changed.validation_fingerprint
    assert original.prompt_canonical_json() != changed.prompt_canonical_json()
