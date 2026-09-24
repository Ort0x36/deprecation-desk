"""Checking whether the provider pages still agree with the catalog.

A first version of this hashed the whole page. Measured against the live
Anthropic page it produced a different digest on three consecutive fetches,
because provider docs carry per-request markup. A tool that cries wolf daily
gets ignored, so it does something narrower and more useful instead: it pulls
the model identifiers and dates out of the page and compares those with the
catalog. A change then means the thing we actually care about changed, and the
tool can say which identifier appeared.

It is deliberately not a full parser. It will not write the catalog for you.
Deciding what a new row means is a human job, and getting it wrong silently is
the failure mode this whole project exists to prevent.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set

from .catalog import Catalog

USER_AGENT = "depdesk/0.2 (+https://github.com/Ort0x36/deprecation-desk)"
TIMEOUT = 20

_SCRIPT = re.compile(rb"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)
_TAG = re.compile(rb"<[^>]+>")

# Identifiers that a provider deprecation page would name.
_IDENTIFIER = re.compile(
    r"\b(?:"
    r"claude-[a-z0-9][a-z0-9.\-]{2,}"
    r"|gpt-[a-z0-9][a-z0-9.\-]{1,}"
    r"|chatgpt-[a-z0-9][a-z0-9.\-]{1,}"
    r"|o[1-9](?:-[a-z0-9][a-z0-9.\-]*)+"
    r"|text-(?:moderation|davinci|curie|babbage|ada)[a-z0-9.\-]*"
    r"|dall-e-[0-9]"
    r"|sora-[0-9][a-z0-9.\-]*"
    r"|whisper-[0-9]"
    r"|babbage-[0-9]{3}|davinci-[0-9]{3}"
    r")\b",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_LONG_DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}\b"
)

# Words that look like an identifier but never are one.
_NOT_IDENTIFIERS = {"claude-api", "gpt-oss"}  # depdesk: ignore


@dataclass
class UpstreamResult:
    provider: str
    url: str
    ok: bool
    digest: Optional[str] = None
    recorded_digest: Optional[str] = None
    verified_on: Optional[str] = None
    changed: Optional[bool] = None
    new_on_page: List[str] = field(default_factory=list)
    missing_from_page: List[str] = field(default_factory=list)
    identifiers_seen: int = 0
    error: Optional[str] = None


def to_text(body: bytes) -> str:
    stripped = _TAG.sub(b" ", _COMMENT.sub(b" ", _SCRIPT.sub(b" ", body)))
    return html.unescape(stripped.decode("utf-8", errors="replace"))


def extract(body: bytes) -> Dict[str, Set[str]]:
    """The identifiers and dates the page names, which is all we care about."""
    text = to_text(body)
    identifiers = {
        match.group(0).lower()
        for match in _IDENTIFIER.finditer(text)
        if match.group(0).lower() not in _NOT_IDENTIFIERS
    }
    dates = set(_ISO_DATE.findall(text)) | set(_LONG_DATE.findall(text))
    return {"identifiers": identifiers, "dates": dates}


def fingerprint(body: bytes) -> str:
    """Stable across cosmetic redeploys, sensitive to a new model or a new date."""
    found = extract(body)
    payload = "\n".join(
        ["ids:", *sorted(found["identifiers"]), "dates:", *sorted(found["dates"])]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return response.read()


def check(catalog: Catalog) -> List[UpstreamResult]:
    results: List[UpstreamResult] = []
    for source in catalog.sources:
        url = source.get("url")
        provider = source.get("provider", "unknown")
        if not url:
            continue
        recorded = source.get("sha256")
        try:
            body = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            results.append(
                UpstreamResult(
                    provider=provider, url=url, ok=False,
                    recorded_digest=recorded, verified_on=source.get("verified_on"),
                    error=str(exc),
                )
            )
            continue

        found = extract(body)
        on_page = found["identifiers"]
        # Only models: the regex cannot see an endpoint or a product name, so
        # comparing those would report a difference that does not exist.
        known = {
            e.id.lower()
            for e in catalog.entries
            if e.provider == provider and e.kind == "model"
        }
        # A model we already name as somebody's replacement is not news either.
        known |= {
            e.replacement.lower()
            for e in catalog.entries
            if e.provider == provider and e.replacement
        }
        digest = fingerprint(body)

        results.append(
            UpstreamResult(
                provider=provider,
                url=url,
                ok=True,
                digest=digest,
                recorded_digest=recorded,
                verified_on=source.get("verified_on"),
                changed=None if recorded is None else digest != recorded,
                new_on_page=sorted(on_page - known),
                missing_from_page=sorted(known - on_page),
                identifiers_seen=len(on_page),
            )
        )
    return results


def save(catalog_path: Path, results: List[UpstreamResult], today: date) -> None:
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_url = {r.url: r for r in results if r.ok and r.digest}
    for source in raw.get("sources", []):
        result = by_url.get(source.get("url"))
        if result is None:
            continue
        source["sha256"] = result.digest
        source["verified_on"] = today.isoformat()
    catalog_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def render(results: List[UpstreamResult], today: date, limit: int = 12) -> str:
    lines: List[str] = []
    for result in results:
        lines.append(f"{result.provider}: {result.url}")
        if not result.ok:
            lines.append(f"  could not fetch: {result.error}")
            lines.append("")
            continue

        lines.append(
            f"  {result.identifiers_seen} identifier(s) on the page, "
            f"fingerprint {result.digest[:16]}"
        )
        if result.recorded_digest is None:
            lines.append("  no fingerprint recorded yet. Run with --save to record this one.")
        elif result.changed:
            lines.append(f"  CHANGED since {result.verified_on}.")
        else:
            lines.append(f"  unchanged since {result.verified_on}.")

        if result.new_on_page:
            shown = result.new_on_page[:limit]
            lines.append(
                f"  review queue: {len(result.new_on_page)} identifier(s) on the page that the"
            )
            lines.append(
                "  catalog does not mention. Expect false positives here, because a"
            )
            lines.append(
                "  deprecation page also names models that are not being deprecated:"
            )
            for identifier in shown:
                lines.append(f"      {identifier}")
            if len(result.new_on_page) > len(shown):
                lines.append(f"      ... and {len(result.new_on_page) - len(shown)} more")
        if result.missing_from_page:
            shown = result.missing_from_page[:limit]
            lines.append(
                f"  in the catalog but NOT on the page ({len(result.missing_from_page)}), "
                "which usually means the provider dropped an old row:"
            )
            for identifier in shown:
                lines.append(f"      {identifier}")
            if len(result.missing_from_page) > len(shown):
                lines.append(f"      ... and {len(result.missing_from_page) - len(shown)} more")
        lines.append("")

    if not results:
        lines.append("The catalog lists no sources to check.")
    return "\n".join(lines).rstrip() + "\n"


def exit_code(results: List[UpstreamResult], strict: bool = False) -> int:
    """1 means a page moved. Anything looser makes the weekly alarm useless.

    The review queue (identifiers the page names and the catalog does not) is
    a standing condition, not an event: the OpenAI page alone names 35 of them
    and always will, because a deprecation page also lists models that are not
    being deprecated. Exiting 1 on that turned the scheduled job into an issue
    every Monday saying the pages changed when they had not, which is how an
    alarm stops being read. A model that genuinely starts being deprecated
    changes the page, and that is what `changed` already catches.
    """
    if any(not r.ok for r in results):
        return 3
    if any(r.changed for r in results):
        return 1
    if strict and any(r.new_on_page or r.missing_from_page for r in results):
        return 1
    return 0
