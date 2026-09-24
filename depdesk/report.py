"""Turning a scan into findings, and findings into something worth reading."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .catalog import Catalog, Entry
from .scan import Hit, ParamHit, ScanResult, rule_for

# Order matters: it is the order findings are printed in.
SEVERITIES = ("retired", "due", "deprecated", "sunset", "review", "unknown")

# What fails the build. Up to 0.1.4 a deprecation outside the --fail-in window
# exited 1, and since CI, the action and pre-commit all fail on any non zero
# code, --fail-in decided nothing: a model retiring in five months broke the
# build exactly like one retiring tomorrow. Now the window is the line, and
# --strict brings the softer findings back in for whoever wants that.
FAILING = ("retired", "due")
WARNING = ("deprecated", "sunset")

_LABELS = {
    "retired": "ALREADY RETIRED",
    "due": "RETIRES SOON",
    "deprecated": "DEPRECATED",
    "sunset": "END OF LIFE ANNOUNCED",
    "review": "NEEDS REVIEW",
    "unknown": "NOT IN CATALOG",
}

_COLORS = {
    "retired": "\033[1;31m",
    "due": "\033[1;33m",
    "deprecated": "\033[33m",
    "sunset": "\033[36m",
    "review": "\033[35m",
    "unknown": "\033[90m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _use_colour(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


@dataclass
class Finding:
    identifier: str
    severity: str
    entry: Optional[Entry]
    days_left: Optional[int]
    locations: List[Hit] = field(default_factory=list)
    usage_count: Optional[float] = None
    usage_share: Optional[float] = None
    detail: Optional[str] = None
    # Set when the provider's recommended replacement is itself deprecated or
    # retired. The page keeps historical rows as they were written, so
    # chatgpt-4o-latest still points at gpt-5.1-chat-latest, which is dead  # depdesk: ignore
    # too, and printing that as the migration target sends people to a
    # second migration.
    replacement_note: Optional[str] = None

    @property
    def replacement(self) -> Optional[str]:
        return self.entry.replacement if self.entry else None


@dataclass
class Report:
    findings: List[Finding]
    param_hits: List[ParamHit]
    catalog: Catalog
    today: date
    scanned: int
    roots: List[Path]
    usage_column: Optional[str] = None
    usage_ignored: List[str] = field(default_factory=list)
    fail_in: int = 90
    strict: bool = False
    skipped: List[Tuple[Path, str]] = field(default_factory=list)
    include_unknown: bool = True

    @property
    def worst(self) -> Optional[str]:
        for severity in SEVERITIES:
            if any(f.severity == severity for f in self.findings):
                return severity
        return None

    def fails(self, finding: "Finding") -> bool:
        return finding.severity in FAILING or (self.strict and finding.severity in WARNING)

    def param_fails(self, hit: ParamHit) -> bool:
        return hit.certain or self.strict

    @property
    def warnings(self) -> int:
        """Findings worth reading that do not fail the build on their own."""
        soft = sum(1 for f in self.findings if f.severity in WARNING)
        soft += sum(1 for h in self.param_hits if not h.certain)
        # A file that could not be read is a place nobody looked.
        soft += len(self.skipped)
        return soft

    def exit_code(self) -> int:
        if any(f.severity in FAILING for f in self.findings):
            return 2
        if any(hit.certain for hit in self.param_hits):
            return 2
        if self.strict and self.warnings:
            return 1
        return 0


def _unknown_detail(identifier: str, embedded: Dict[str, str]) -> str:
    inner = embedded.get(identifier)
    if inner is None:
        return "Not present in the catalog, so nothing can be said about it."
    # The provider date would be a guess here: Bedrock, Vertex and Azure run
    # their own retirement schedules, and the Anthropic page says so itself.
    return (
        f"Contains {inner}, but written this way it is probably a cloud platform "
        "or deployment name, which follows that platform's own retirement schedule. "
        "Check it there."
    )


def _replacement_note(entry: Entry, index: Dict[str, Entry]) -> Optional[str]:
    """Follow the replacement while it points at something that is dying too."""
    first = index.get(entry.replacement or "")
    if first is None or first.status == "active":
        return None
    current, seen = first, {entry.id}
    while current.replacement and current.replacement not in seen:
        seen.add(current.id)
        following = index.get(current.replacement)
        if following is None or following.status == "active":
            return f"itself {first.status}; its replacement is {current.replacement}"
        current = following
    return f"itself {first.status}, with no living replacement in the catalog"


def build(
    scan_result: ScanResult,
    catalog: Catalog,
    today: date,
    fail_in: int,
    sunset_in: int,
    usage: Optional[Dict[str, float]] = None,
    usage_shares: Optional[Dict[str, float]] = None,
    usage_column: Optional[str] = None,
    roots: Optional[List[Path]] = None,
    include_unknown: bool = True,
    strict: bool = False,
) -> Report:
    index = catalog.by_id
    grouped = dict(scan_result.by_identifier())

    # Identifiers that look like a model but are absent from the catalog are
    # findings too: "we cannot tell you" is an answer, silence is not.
    if include_unknown:
        for candidate, hits in scan_result.unknown.items():
            grouped.setdefault(candidate, []).extend(hits)

    # Anything named in code or seen in real traffic deserves a verdict.
    candidates = set(grouped)
    if usage:
        candidates.update(usage)

    findings: List[Finding] = []
    usage_ignored: List[str] = []

    for identifier in sorted(candidates):
        entry = index.get(identifier)
        locations = grouped.get(identifier, [])
        count = usage.get(identifier) if usage else None
        share = usage_shares.get(identifier) if usage_shares else None

        if entry is None:
            if usage and identifier in usage and not locations:
                usage_ignored.append(identifier)
            if not include_unknown:
                continue
            findings.append(
                Finding(
                    identifier=identifier,
                    severity="unknown",
                    entry=None,
                    days_left=None,
                    locations=locations,
                    usage_count=count,
                    usage_share=share,
                    detail=_unknown_detail(identifier, scan_result.embedded),
                )
            )
            continue

        days = entry.days_left(today)
        if entry.status == "retired" or (days is not None and days < 0):
            severity = "retired"
        elif days is not None and days <= fail_in:
            severity = "due"
        elif entry.status == "deprecated":
            severity = "deprecated"
        else:
            until = entry.days_until_earliest(today)
            # 0 is documented as off. It was not: from the earliest retirement
            # date on, every default run reported a sunset nobody asked for.
            if until is not None and sunset_in > 0 and until <= sunset_in:
                severity = "sunset"
                days = until
            else:
                continue  # Active, with no announced date close enough to matter.

        findings.append(
            Finding(
                identifier=identifier,
                severity=severity,
                entry=entry,
                days_left=days,
                locations=locations,
                usage_count=count,
                usage_share=share,
                replacement_note=_replacement_note(entry, index),
            )
        )

    order = {name: position for position, name in enumerate(SEVERITIES)}
    findings.sort(
        key=lambda f: (
            order.get(f.severity, 99),
            f.days_left if f.days_left is not None else 10**6,
            f.identifier,
        )
    )

    return Report(
        findings=findings,
        param_hits=scan_result.param_hits,
        catalog=catalog,
        today=today,
        scanned=scan_result.files_scanned,
        roots=roots or [],
        usage_column=usage_column,
        usage_ignored=usage_ignored,
        fail_in=fail_in,
        strict=strict,
        skipped=list(scan_result.skipped),
        include_unknown=include_unknown,
    )


def _short(path: Path) -> str:
    """Relative to where the user is standing, when that is shorter."""
    try:
        relative = os.path.relpath(path, Path.cwd())
    except (ValueError, OSError):
        return str(path)
    return relative if len(relative) < len(str(path)) else str(path)


def _deadline(finding: Finding) -> str:
    entry = finding.entry
    if entry is None:
        return "unknown"
    if finding.severity == "sunset" and entry.earliest_retirement:
        when = entry.earliest_retirement.isoformat()
        if finding.days_left is not None and finding.days_left < 0:
            return f"not sooner than {when} (passed {abs(finding.days_left)} days ago)"
        return f"not sooner than {when} ({finding.days_left} days)"
    if entry.retires_on is None:
        return entry.retirement_note or "no retirement date announced"
    if finding.days_left is None:
        return entry.retires_on.isoformat()
    if finding.days_left < 0:
        return f"{entry.retires_on.isoformat()} ({abs(finding.days_left)} days ago)"
    return f"{entry.retires_on.isoformat()} ({finding.days_left} days left)"


def render_text(report: Report, show_locations: int = 3, stream=None) -> str:
    stream = stream or sys.stdout
    colour = _use_colour(stream)

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if colour else text

    lines: List[str] = []
    age = report.catalog.age_days(report.today)
    header = f"depdesk: {report.scanned} files scanned"
    if report.roots:
        header += " in " + ", ".join(_short(r) for r in report.roots)
    lines.append(header)
    verified = report.catalog.verified_on.isoformat()
    lines.append(
        paint(
            f"catalog verified {verified} ({age} days ago)" if age >= 0
            else f"catalog verified {verified}, after the date asked about",
            _DIM if colour else "",
        )
    )
    if age > 30:
        lines.append(
            paint(
                "  warning: this catalog is over 30 days old. Run `depdesk upstream`"
                " before trusting it.",
                _COLORS["due"],
            )
        )
    lines.append("")

    if report.skipped:
        lines.append(paint("COULD NOT READ", _COLORS["due"]))
        lines.append("")
        for path, reason in report.skipped[:10]:
            lines.append(f"  {_short(path)}: {reason}")
        if len(report.skipped) > 10:
            lines.append(f"  ... and {len(report.skipped) - 10} more")
        lines.append("")

    if report.scanned == 0 and not report.findings:
        lines.append("None of the paths given is a file depdesk reads, so there was nothing to check.")
        return "\n".join(lines) + "\n"
    if not report.findings and not report.param_hits:
        if report.usage_ignored and not report.include_unknown:
            lines.append(
                f"{len(report.usage_ignored)} model(s) in your usage data are not in the catalog "
                "and were left out because of --no-unknown."
            )
        if report.skipped:
            lines.append("Nothing deprecated found in the files that were read. The ones above")
            lines.append("were not, so this is not a clean bill for the tree. --strict fails on them.")
        else:
            lines.append("Nothing deprecated found. Every model identifier in this tree is active")
            lines.append("with no retirement date inside the window you asked about.")
        return "\n".join(lines) + "\n"

    current = None
    for finding in report.findings:
        if finding.severity != current:
            current = finding.severity
            lines.append(paint(_LABELS[finding.severity], _COLORS[finding.severity]))
            lines.append("")

        head = f"  {finding.identifier}"
        if finding.usage_share is not None:
            head += f"  [{finding.usage_share * 100:.1f}% of measured calls]"
        lines.append(head)

        if finding.entry is not None:
            lines.append(f"      provider:    {finding.entry.provider}")
            lines.append(f"      retirement:  {_deadline(finding)}")
            if finding.entry.deprecated_on:
                lines.append(
                    f"      announced:   {finding.entry.deprecated_on.isoformat()}"
                )
            if finding.replacement:
                note = f" ({finding.replacement_note})" if finding.replacement_note else ""
                lines.append(f"      replacement: {finding.replacement}{note}")
            if finding.entry.note:
                lines.append(f"      note:        {finding.entry.note}")
        if finding.detail:
            lines.append(f"      {finding.detail}")

        if finding.locations:
            shown = finding.locations[:show_locations]
            for hit in shown:
                lines.append(f"      {_short(hit.path)}:{hit.line}: {hit.excerpt}")
            remaining = len(finding.locations) - len(shown)
            if remaining > 0:
                lines.append(f"      ... and {remaining} more occurrence(s)")
        elif finding.usage_count is not None:
            lines.append("      seen in usage data but not found anywhere in the scanned tree")
        lines.append("")

    if report.param_hits:
        lines.append(paint("DEPRECATED PARAMETERS", _COLORS["review"]))
        lines.append("")
        grouped: Dict[str, List[ParamHit]] = {}
        for hit in report.param_hits:
            grouped.setdefault(hit.parameter, []).append(hit)
        for name, hits in sorted(grouped.items()):
            rule = rule_for(report.catalog, name)
            certain = any(h.certain for h in hits)
            suffix = "" if certain else "  (scope uncertain, review)"
            lines.append(f"  {name}{suffix}")
            if rule:
                lines.append(f"      behaviour:   {rule.behavior}")
                if rule.replacement:
                    lines.append(f"      replacement: {rule.replacement}")
                if rule.sdk_note:
                    lines.append(f"      sdk:         {rule.sdk_note}")
                if not certain and rule.uncertainty_note:
                    lines.append(f"      caveat:      {rule.uncertainty_note}")
            for hit in hits[:show_locations]:
                models = ", ".join(hit.models_in_file)
                lines.append(f"      {_short(hit.path)}:{hit.line}: {hit.excerpt}")
                lines.append(f"          file also names: {models}")
            remaining = len(hits) - min(len(hits), show_locations)
            if remaining > 0:
                lines.append(f"      ... and {remaining} more occurrence(s)")
            lines.append("")

    if report.usage_ignored:
        where = (
            "and were reported as NOT IN CATALOG." if report.include_unknown
            else "and were left out because of --no-unknown."
        )
        lines.append(
            f"{len(report.usage_ignored)} model(s) in your usage data are not in the catalog {where}"
        )
        lines.append("")

    counts: Dict[str, int] = {}
    for finding in report.findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [f"{count} {_LABELS[sev].lower()}" for sev, count in
             sorted(counts.items(), key=lambda kv: SEVERITIES.index(kv[0]))]
    # A run that found only a parameter printed "nothing to report" and then
    # exited 2.
    if report.param_hits:
        parts.append(f"{len(report.param_hits)} deprecated parameter use(s)")
    if report.skipped:
        parts.append(f"{len(report.skipped)} file(s) not read")
    lines.append(f"Summary: {', '.join(parts) or 'nothing to report'}.")
    if report.warnings and report.exit_code() == 0:
        # Without this line a list of deprecations followed by a green build
        # reads like a bug in the tool.
        lines.append(
            f"Nothing above retires within {report.fail_in} days or is certain to break, "
            "so the build passes. Pass --strict to fail on it too."
        )
    return "\n".join(lines) + "\n"


def render_json(report: Report) -> str:
    payload = {
        "generated_on": report.today.isoformat(),
        "catalog": {
            "path": str(report.catalog.path),
            "verified_on": report.catalog.verified_on.isoformat(),
            "age_days": report.catalog.age_days(report.today),
            "sources": report.catalog.sources,
        },
        "scanned_files": report.scanned,
        "roots": [str(r) for r in report.roots],
        "exit_code": report.exit_code(),
        "fail_in": report.fail_in,
        "strict": report.strict,
        "findings": [
            {
                "identifier": f.identifier,
                "severity": f.severity,
                "fails_build": report.fails(f),
                "provider": f.entry.provider if f.entry else None,
                "kind": f.entry.kind if f.entry else None,
                "status": f.entry.status if f.entry else None,
                "deprecated_on": f.entry.deprecated_on.isoformat()
                if f.entry and f.entry.deprecated_on else None,
                "retires_on": f.entry.retires_on.isoformat()
                if f.entry and f.entry.retires_on else None,
                "earliest_retirement": f.entry.earliest_retirement.isoformat()
                if f.entry and f.entry.earliest_retirement else None,
                "days_left": f.days_left,
                "replacement": f.replacement,
                "replacement_note": f.replacement_note,
                "usage_count": f.usage_count,
                "usage_share": f.usage_share,
                "detail": f.detail,
                "locations": [
                    {"path": str(h.path), "line": h.line, "excerpt": h.excerpt}
                    for h in f.locations
                ],
            }
            for f in report.findings
        ],
        "skipped": [{"path": str(path), "reason": reason} for path, reason in report.skipped],
        "parameters": [
            {
                "parameter": h.parameter,
                "path": str(h.path),
                "line": h.line,
                "excerpt": h.excerpt,
                "models_in_file": h.models_in_file,
                "scope_certain": h.certain,
                "fails_build": report.param_fails(h),
            }
            for h in report.param_hits
        ],
    }
    # ASCII escapes survive any stdout encoding; the characters themselves
    # turned into "?" on a Windows pipe.
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"
