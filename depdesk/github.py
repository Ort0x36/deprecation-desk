"""Findings where a GitHub pull request shows them, not only in the job log.

A failing step says that something is wrong. Nobody opens the log of a green
one, and since warnings no longer fail the build, a deprecation five months
out would otherwise live only in a log nobody reads. Inside GitHub Actions the
findings also become annotations, which show on the changed line of the pull
request, and a table in the job summary.

This turns itself on when GITHUB_ACTIONS is "true", so the plain `pip install`
workflow in the README gets it without knowing it exists. `--no-github` turns
it off.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, TextIO

from .report import _LABELS, Report, _deadline
from .scan import rule_for


def enabled(environ: Optional[dict] = None) -> bool:
    return (environ if environ is not None else os.environ).get("GITHUB_ACTIONS") == "true"


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _relative(path: Path) -> str:
    # Annotations only land on a file when the path is relative to the
    # checkout. Pre-commit passes relative paths already; `depdesk check .`
    # from a subdirectory, or an absolute path, would not.
    workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()
    try:
        relative = os.path.relpath(path.resolve(), Path(workspace).resolve())
    except (ValueError, OSError):
        return str(path)
    return str(path) if relative.startswith("..") else relative


def _command(level: str, title: str, message: str, path: Optional[Path] = None,
             line: Optional[int] = None) -> str:
    props = []
    if path is not None:
        props.append(f"file={_escape_property(_relative(path))}")
        if line is not None:
            props.append(f"line={line}")
    props.append(f"title={_escape_property(title)}")
    return f"::{level} {','.join(props)}::{_escape_data(message)}"


def annotations(report: Report, limit: int = 3) -> List[str]:
    commands: List[str] = []
    for finding in report.findings:
        if finding.entry is None:
            # Identifiers the catalog does not know stay in the log. As
            # annotations they would bury the real ones: GitHub shows only ten
            # of each level per step.
            continue
        level = "error" if report.fails(finding) else "warning"
        title = f"depdesk: {_LABELS[finding.severity].lower()}"
        message = f"{finding.identifier} ({finding.entry.provider}), retirement {_deadline(finding)}."
        if finding.replacement:
            message += f" Replacement: {finding.replacement}."
            if finding.replacement_note:
                message += f" That one is {finding.replacement_note}."
        if finding.usage_share is not None:
            message += f" {finding.usage_share * 100:.1f}% of measured calls."
        if finding.locations:
            # --locations only trims the printed report. With 0 it used to
            # remove every annotation from a build that was failing.
            for hit in finding.locations[:max(limit, 1)]:
                commands.append(_command(level, title, message, hit.path, hit.line))
        else:
            commands.append(_command(level, title, message + " Seen in usage data only."))

    for hit in report.param_hits:
        level = "error" if report.param_fails(hit) else "warning"
        rule = rule_for(report.catalog, hit.parameter)
        message = f"{hit.parameter} on {', '.join(hit.models_in_file)}."
        if rule:
            message += f" {rule.behavior}"
            if rule.replacement:
                message += f" {rule.replacement}"
        if not hit.certain:
            message += " The provider's wording leaves the scope uncertain."
        commands.append(
            _command(level, "depdesk: deprecated parameter", message, hit.path, hit.line)
        )
    for path, reason in report.skipped:
        commands.append(_command("warning", "depdesk: file not read", f"Not scanned: {reason}.", path))
    return commands


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def summary(report: Report) -> str:
    lines = ["### depdesk", ""]
    lines.append(
        f"{report.scanned} files scanned, catalog verified "
        f"{report.catalog.verified_on.isoformat()}."
    )
    lines.append("")

    known = [f for f in report.findings if f.entry is not None]
    if known:
        lines.append("| | model | retirement | replacement | where |")
        lines.append("| --- | --- | --- | --- | --- |")
        for finding in known:
            verdict = "fails" if report.fails(finding) else "warns"
            where = (
                f"`{_cell(_relative(finding.locations[0].path))}:{finding.locations[0].line}`"
                if finding.locations else "usage data only"
            )
            if len(finding.locations) > 1:
                where += f" and {len(finding.locations) - 1} more"
            lines.append(
                f"| {verdict} | `{finding.identifier}` "
                f"| {_LABELS[finding.severity].lower()}, {_cell(_deadline(finding))} "
                f"| {'`' + finding.replacement + '`' if finding.replacement else ''} "
                f"| {where} |"
            )
        lines.append("")

    if report.param_hits:
        lines.append("| | parameter | where | models in the file |")
        lines.append("| --- | --- | --- | --- |")
        for hit in report.param_hits:
            verdict = "fails" if report.param_fails(hit) else "warns"
            lines.append(
                f"| {verdict} | `{hit.parameter}` | `{_cell(_relative(hit.path))}:{hit.line}` "
                f"| {', '.join(hit.models_in_file)} |"
            )
        lines.append("")

    if report.skipped:
        lines.append(f"{len(report.skipped)} file(s) could not be read and were not scanned:")
        lines.append("")
        for path, reason in report.skipped[:10]:
            lines.append(f"- `{_cell(_relative(path))}`: {reason}")
        if len(report.skipped) > 10:
            lines.append(f"- and {len(report.skipped) - 10} more")
        lines.append("")

    unknown = len(report.findings) - len(known)
    if unknown:
        lines.append(f"{unknown} identifier(s) not in the catalog are listed in the job log.")
        lines.append("")

    if not known and not report.param_hits and not report.skipped:
        lines.append("Nothing deprecated found.")
    elif report.exit_code() == 0:
        lines.append(
            f"Nothing here retires within {report.fail_in} days or is certain to break, "
            "so the build passes."
        )
    else:
        lines.append(f"Exit code {report.exit_code()}: the build fails.")
    return "\n".join(lines) + "\n"


def emit(report: Report, stream: TextIO, limit: int = 3, annotate: bool = True) -> None:
    if annotate:
        for command in annotations(report, limit):
            stream.write(command + "\n")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(summary(report) + "\n")
        except OSError:
            # A summary that cannot be written is not a reason to change the
            # verdict of the check itself.
            pass
