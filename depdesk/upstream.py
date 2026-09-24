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
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from . import __version__
from .catalog import Catalog

USER_AGENT = f"depdesk/{__version__} (+https://github.com/Ort0x36/deprecation-desk)"
TIMEOUT = 20

_SCRIPT = re.compile(rb"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)
_TAG = re.compile(rb"<[^>]+>")

# Identifiers that a provider deprecation page would name. The boundaries are
# the scanner's, not \b: with \b, text-similarity-babbage-001 also produced a  # depdesk: ignore
# babbage-001 that exists nowhere. gpt-4 and o1 are allowed bare, because the  # depdesk: ignore
# catalog carries them and the old pattern could not see them, which made them
# look permanently missing from the page.
_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?:"
    r"claude-[a-z0-9][a-z0-9.\-]{2,}"
    r"|gpt-[a-z0-9][a-z0-9.\-]*"
    r"|chatgpt-[a-z0-9][a-z0-9.\-]{1,}"
    r"|o[1-9](?:-[a-z0-9][a-z0-9.\-]*)*"
    r"|text-(?:moderation|davinci|curie|babbage|ada|similarity|search)[a-z0-9.\-]*"
    r"|code-(?:davinci|cushman|search)[a-z0-9.\-]*"
    r"|codex-[a-z0-9][a-z0-9.\-]*"
    r"|computer-use-[a-z0-9][a-z0-9.\-]*"
    r"|omni-moderation[a-z0-9.\-]*"
    r"|dall-e-[0-9]"
    r"|sora-[0-9][a-z0-9.\-]*"
    r"|whisper-[0-9]"
    r"|babbage-[0-9]{3}|davinci-[0-9]{3}"
    r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Dates in every form the two pages use. The OpenAI tables write "Aug 10,
# 2026" and a few ISO dates with U+2011 non-breaking hyphens, and neither was
# extracted, so those rows could change date without the drift job noticing.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_LONG_DATE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})\b"
)

_TOKENS = re.compile(
    "(" + _IDENTIFIER.pattern + ")|" + _ISO_DATE.pattern + "|" + _LONG_DATE.pattern, re.IGNORECASE
)

_HYPHENS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", " ": " "})

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
    note: Optional[str] = None


def to_text(body: bytes) -> str:
    stripped = _TAG.sub(b" ", _COMMENT.sub(b" ", _SCRIPT.sub(b" ", body)))
    return html.unescape(stripped.decode("utf-8", errors="replace")).translate(_HYPHENS)


def article(text: str, between: Optional[List[str]]) -> Tuple[str, bool]:
    """The part of the page that is the deprecation list, and whether it was found.

    Navigation carries model names too: the OpenAI sidebar lists recent blog
    posts, so a new post title changed the fingerprint and would have opened a
    drift issue about a page that had not moved. When the markers are missing
    the whole page is used, and the caller reports that, because it means the
    page itself was restructured.
    """
    if not between:
        return text, True
    start, end = between
    # Each marker has to be unique. An end marker repeated inside the list cut
    # the tail off without a word, and after one --save that part of the page
    # was no longer watched.
    if text.count(start) != 1 or text.count(end) != 1:
        return text, False
    begin = text.find(start)
    finish = text.find(end, begin + 1)
    if finish < 0:
        return text, False
    return text[begin:finish], True


def tokens(text: str) -> List[str]:
    """Identifiers and dates in the order the page gives them.

    The order is the point. Hashing the set of identifiers and the set of
    dates separately missed a row moving to a date that already appeared
    elsewhere on the page, which is most shutdown changes: the page is full of
    the same few dates.
    """
    found: List[str] = []
    for match in _TOKENS.finditer(text):
        if match.group(1):
            ident = match.group(1).lower().rstrip(".-")
            if ident not in _NOT_IDENTIFIERS:
                found.append("id:" + ident)
            continue
        iso = _iso_from_groups(match.groups()[1:])
        if iso:
            found.append("date:" + iso)
    return found


def _iso_from_groups(groups: Tuple[Optional[str], ...]) -> Optional[str]:
    iso_year, iso_month, iso_day, month_name, day, year = groups
    try:
        if iso_year is not None:
            return date(int(iso_year), int(iso_month), int(iso_day)).isoformat()
        key = month_name.lower().rstrip(".")
        key = "sept" if key.startswith("sept") else key[:3]
        return date(int(year), _MONTHS[key], int(day)).isoformat()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def extract(body: bytes, between: Optional[List[str]] = None) -> Dict[str, Set[str]]:
    """The identifiers and dates the page names, which is all we care about."""
    text, _ = article(to_text(body), between)
    found = tokens(text)
    return {
        "identifiers": {t[3:] for t in found if t.startswith("id:")},
        "dates": {t[5:] for t in found if t.startswith("date:")},
    }


def fingerprint(body: bytes, between: Optional[List[str]] = None) -> str:
    """Stable across cosmetic redeploys, sensitive to a new model or a moved date."""
    text, _ = article(to_text(body), between)
    return hashlib.sha256("\n".join(tokens(text)).encode("utf-8")).hexdigest()


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
        between = source.get("between")
        try:
            body = fetch(url)
        except Exception as exc:  # noqa: BLE001
            # Anything that stops the fetch, including a bad URL or a
            # truncated response, is "could not check" (exit 3). An uncaught
            # one exited 1, which the drift workflow reads as "the page moved"
            # and files an issue asking for the catalog to be retranscribed.
            results.append(
                UpstreamResult(
                    provider=provider, url=url, ok=False,
                    recorded_digest=recorded, verified_on=source.get("verified_on"),
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            )
            continue

        text, located = article(to_text(body), between)
        found = tokens(text)
        on_page = {t[3:] for t in found if t.startswith("id:")}
        if not on_page:
            # An empty JavaScript shell, or a bot challenge served with 200.
            # Saved as a baseline, it made every later check pass.
            results.append(
                UpstreamResult(
                    provider=provider, url=url, ok=False,
                    recorded_digest=recorded, verified_on=source.get("verified_on"),
                    error="the page names no model at all, so it is not the deprecation page "
                          "(blocked, empty, or moved)",
                )
            )
            continue

        # Only models: the regex cannot see an endpoint or a product name, so
        # comparing those would report a difference that does not exist.
        known = {
            e.id.lower()
            for e in catalog.entries
            if e.provider == provider and e.kind == "model" and e.source in (None, url)
        }
        # A model we already name as somebody's replacement is not news either.
        known |= {
            e.replacement.lower()
            for e in catalog.entries
            if e.provider == provider and e.replacement
        }
        digest = hashlib.sha256("\n".join(found).encode("utf-8")).hexdigest()

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
                missing_from_page=sorted(i for i in known - on_page if _IDENTIFIER.fullmatch(i)),
                identifiers_seen=len(on_page),
                note=None if located else "the markers around the deprecation list were not "
                                          "found exactly once, so the page layout changed; the "
                                          "whole page was used",
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
            lines.append(f"  could not check: {result.error}")
            lines.append("")
            continue

        lines.append(
            f"  {result.identifiers_seen} identifier(s) on the page, "
            f"fingerprint {result.digest[:16]}"
        )
        if result.note:
            lines.append(f"  note: {result.note}")
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
    a standing condition, not an event: a deprecation page also lists models
    that are not being deprecated, and always will. Exiting 1 on that turned
    the scheduled job into an issue every Monday saying the pages changed when
    they had not, which is how an alarm stops being read. A model that
    genuinely starts being deprecated changes the page, and that is what
    `changed` already catches.
    """
    if any(not r.ok for r in results):
        return 3
    if any(r.changed for r in results):
        return 1
    if strict and any(r.new_on_page or r.missing_from_page for r in results):
        return 1
    return 0
