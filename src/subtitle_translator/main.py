from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from subtitle_translator.batch import BatchTranslator
from subtitle_translator.config import get_settings
from subtitle_translator.glossary import (
    GlossaryDocument,
    atomic_write_glossary,
    load_glossary,
    remove_term,
    set_term,
)
from subtitle_translator.mounting import ensure_media_mount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle-translator",
        description="Scan completed extraction jobs once and publish translated subtitles.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("scan", help="Scan once, translate pending jobs, then exit")
    one_parser = subparsers.add_parser(
        "translate-one",
        help="Translate one job selected by its media-relative path",
    )
    one_parser.add_argument(
        "media_relative_path",
        nargs="?",
        help="Optional exact source.relative_path; omit to select the first pending job",
    )
    subparsers.add_parser("api", help="Start the optional FastAPI service")
    glossary_parser = subparsers.add_parser(
        "glossary", help="Inspect or maintain the versioned glossary"
    )
    glossary_commands = glossary_parser.add_subparsers(dest="glossary_command", required=True)
    glossary_commands.add_parser("list", help="List glossary terms")
    set_parser = glossary_commands.add_parser("set", help="Add or update a glossary term")
    set_parser.add_argument("source")
    set_parser.add_argument("target")
    remove_parser = glossary_commands.add_parser("remove", help="Remove a glossary term")
    remove_parser.add_argument("source")
    glossary_commands.add_parser("validate", help="Validate the glossary file")
    import_parser = glossary_commands.add_parser(
        "import", help="Import a versioned or legacy flat JSON glossary"
    )
    import_parser.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    if args.command == "glossary":
        return _run_glossary_command(args, settings.glossary_path)

    try:
        ensure_media_mount(settings)
    except Exception as exc:
        logging.getLogger(__name__).exception("Media mount check failed")
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2

    if args.command == "api":
        uvicorn.run(
            "subtitle_translator.api:app",
            host=settings.host,
            port=settings.port,
            reload=False,
        )
        return 0

    try:
        translator = BatchTranslator(settings)
        summary = (
            translator.run_one(args.media_relative_path)
            if args.command == "translate-one"
            else translator.run()
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("Batch translation scan failed")
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps({"status": "complete", **summary.as_dict()}, ensure_ascii=False))
    return 1 if summary.failed else 0


def _run_glossary_command(args: argparse.Namespace, destination: Path) -> int:
    try:
        if args.glossary_command == "import":
            glossary = load_glossary(args.path)
            atomic_write_glossary(destination, glossary)
        else:
            if destination.expanduser().exists():
                glossary = load_glossary(destination)
            elif args.glossary_command == "set":
                glossary = GlossaryDocument()
            else:
                raise ValueError(f"Glossary file does not exist: {destination}")

            if args.glossary_command == "set":
                glossary = set_term(glossary, args.source, args.target)
                atomic_write_glossary(destination, glossary)
            elif args.glossary_command == "remove":
                glossary = remove_term(glossary, args.source)
                atomic_write_glossary(destination, glossary)

        result = {
            "status": "complete",
            "count": len(glossary.terms),
            "fingerprint": glossary.fingerprint,
        }
        if args.glossary_command == "list":
            result["glossary"] = glossary.model_dump(mode="json")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, TypeError, ValueError) as exc:
        logging.getLogger(__name__).error("Glossary command failed: %s", exc)
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
