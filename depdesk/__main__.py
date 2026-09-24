"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, github
from .catalog import CatalogError, load
from .report import build, render_json, render_text
from .scan import scan
from .upstream import check, exit_code as upstream_exit_code, render as render_upstream, save
from .usage import UsageError, load_usage, share

EPILOG = """\
exit codes:
  0  nothing fails the build; deprecations further out than --fail-in are
     still printed, as warnings
  1  only with --strict: a warning that would otherwise pass
  2  something is already retired, retires within --fail-in, or passes a
     parameter the model rejects
  3  the tool could not do its job (bad catalog, unreadable usage file)

The exit codes are the point: put `depdesk check` in CI and the build starts
failing while there is still time to migrate, not the day the model dies.
"""


def _add_check_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths", nargs="*", default=["."], help="files or directories to scan (default: .)"
    )
    parser.add_argument(
        "--usage",
        metavar="FILE",
        help="CSV or JSON of real call volume per model, for example the export from "
             "the Anthropic Console usage page",
    )
    parser.add_argument("--usage-model-column", metavar="NAME", help="override the model column")
    parser.add_argument("--usage-count-column", metavar="NAME", help="override the column to sum")
    parser.add_argument(
        "--fail-in",
        type=int,
        default=90,
        metavar="DAYS",
        help="treat a retirement inside this many days as failing (default: 90)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also exit 1 on warnings: deprecations outside --fail-in, announced "
             "sunsets and parameters whose scope is uncertain",
    )
    parser.add_argument(
        "--sunset-in",
        type=int,
        default=0,
        metavar="DAYS",
        help="also report active models whose earliest announced retirement is inside "
             "this many days (default: 0, which is off)",
    )
    parser.add_argument(
        "--locations",
        type=int,
        default=3,
        metavar="N",
        help="how many source locations to print per finding (default: 3)",
    )
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument(
        "--no-github",
        action="store_true",
        help="inside GitHub Actions, do not write annotations or the job summary",
    )
    parser.add_argument(
        "--no-unknown",
        action="store_true",
        help="do not report identifiers that are absent from the catalog",
    )
    parser.add_argument(
        "--exclude",
        metavar="GLOB",
        action="append",
        default=[],
        help="skip paths matching this glob; repeatable. A single line can also "
             "be silenced with a `depdesk: ignore` comment",
    )
    parser.add_argument("--catalog", metavar="FILE", help="use a different catalog file")
    parser.add_argument(
        "--today",
        metavar="YYYY-MM-DD",
        help="pretend today is this date, for testing and for asking what happens later",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="depdesk",
        description="Cross the model deprecation notices you did not read with the "
                    "models your code actually calls.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"depdesk {__version__}")
    sub = parser.add_subparsers(dest="command")

    check_parser = sub.add_parser("check", help="scan a tree and report what is dying")
    _add_check_arguments(check_parser)

    upstream_parser = sub.add_parser(
        "upstream", help="ask whether the provider pages changed since the catalog was verified"
    )
    upstream_parser.add_argument("--catalog", metavar="FILE")
    upstream_parser.add_argument(
        "--save", action="store_true", help="record the current fingerprints in the catalog"
    )
    upstream_parser.add_argument("--json", action="store_true")
    upstream_parser.add_argument(
        "--strict",
        action="store_true",
        help="also exit 1 when the page names identifiers the catalog does not, "
             "which is a standing difference rather than a change",
    )

    list_parser = sub.add_parser("list", help="print the catalog")
    list_parser.add_argument("--catalog", metavar="FILE")
    list_parser.add_argument("--provider", help="only this provider")
    list_parser.add_argument(
        "--status", choices=["active", "deprecated", "retired"], help="only this status"
    )
    list_parser.add_argument("--json", action="store_true")

    return parser


def _today(raw: Optional[str]) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"depdesk: --today is not an ISO date: {raw}")


def cmd_check(args: argparse.Namespace) -> int:
    try:
        catalog = load(args.catalog)
    except CatalogError as exc:
        print(f"depdesk: {exc}", file=sys.stderr)
        return 3

    roots = [Path(p) for p in args.paths]
    missing = [p for p in roots if not p.exists()]
    if missing:
        for path in missing:
            print(f"depdesk: no such path: {path}", file=sys.stderr)
        return 3

    totals = None
    shares = None
    column = None
    if args.usage:
        try:
            totals, column = load_usage(
                Path(args.usage), args.usage_model_column, args.usage_count_column
            )
        except UsageError as exc:
            print(f"depdesk: {exc}", file=sys.stderr)
            return 3
        shares = share(totals)

    # Never scan the catalog itself (it is a list of dead models by
    # definition) nor the usage export, which is data and not code.
    exclude = {catalog.path}
    if args.usage:
        exclude.add(Path(args.usage))
    today = _today(args.today)
    result = scan(roots, catalog, exclude=exclude, exclude_globs=args.exclude, today=today)
    report = build(
        result,
        catalog,
        today=today,
        fail_in=args.fail_in,
        sunset_in=args.sunset_in,
        usage=totals,
        usage_shares=shares,
        usage_column=column,
        roots=roots,
        include_unknown=not args.no_unknown,
        strict=args.strict,
    )

    if args.json:
        sys.stdout.write(render_json(report))
    else:
        sys.stdout.write(render_text(report, show_locations=args.locations))
    if github.enabled() and not args.no_github:
        # No annotations in JSON mode: they go to stdout, and a workflow that
        # pipes the JSON into jq would choke on them.
        github.emit(report, sys.stdout, limit=args.locations, annotate=not args.json)
    return report.exit_code()


def cmd_upstream(args: argparse.Namespace) -> int:
    try:
        catalog = load(args.catalog)
    except CatalogError as exc:
        print(f"depdesk: {exc}", file=sys.stderr)
        return 3

    results = check(catalog)
    if args.json:
        import json as _json

        sys.stdout.write(
            _json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False) + "\n"
        )
    else:
        sys.stdout.write(render_upstream(results, date.today()))

    if args.save:
        fetched = [r for r in results if r.ok]
        if not fetched:
            print("depdesk: nothing fetched, catalog left alone", file=sys.stderr)
            return 3
        save(catalog.path, results, date.today())
        print(f"depdesk: fingerprints written to {catalog.path}", file=sys.stderr)
        return 0

    return upstream_exit_code(results, strict=args.strict)


def cmd_list(args: argparse.Namespace) -> int:
    try:
        catalog = load(args.catalog)
    except CatalogError as exc:
        print(f"depdesk: {exc}", file=sys.stderr)
        return 3

    entries = catalog.entries
    if args.provider:
        entries = [e for e in entries if e.provider == args.provider]
    if args.status:
        entries = [e for e in entries if e.status == args.status]

    if args.json:
        import json as _json

        payload = [
            {
                "id": e.id,
                "provider": e.provider,
                "kind": e.kind,
                "status": e.status,
                "deprecated_on": e.deprecated_on.isoformat() if e.deprecated_on else None,
                "retires_on": e.retires_on.isoformat() if e.retires_on else None,
                "earliest_retirement": e.earliest_retirement.isoformat()
                if e.earliest_retirement else None,
                "replacement": e.replacement,
            }
            for e in entries
        ]
        sys.stdout.write(_json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0

    width = max((len(e.id) for e in entries), default=0)
    print(f"catalog verified {catalog.verified_on.isoformat()}  ({len(entries)} entries)")
    print()
    for entry in sorted(entries, key=lambda e: (e.provider, e.status, e.id)):
        when = (
            entry.retires_on.isoformat()
            if entry.retires_on
            else (f"not sooner than {entry.earliest_retirement.isoformat()}"
                  if entry.earliest_retirement else "no date")
        )
        replacement = f"  ->  {entry.replacement}" if entry.replacement else ""
        print(f"  {entry.id:<{width}}  {entry.provider:<10}  {entry.status:<10}  {when}{replacement}")
    return 0


COMMANDS = ("check", "upstream", "list")


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw: List[str] = list(sys.argv[1:] if argv is None else argv)

    # `depdesk .` and bare `depdesk` mean `depdesk check`. This has to happen
    # before argparse sees the arguments, because otherwise a path in the
    # subcommand position is an "invalid choice" and the process dies.
    if not raw or (raw[0] not in COMMANDS and not raw[0].startswith("-")):
        raw = ["check", *raw]
    elif raw and raw[0].startswith("-") and raw[0] not in ("-h", "--help", "--version"):
        raw = ["check", *raw]

    parser = build_parser()
    args = parser.parse_args(raw)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "upstream":
        return cmd_upstream(args)
    if args.command == "list":
        return cmd_list(args)
    parser.print_help()
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
