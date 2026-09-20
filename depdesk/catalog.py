"""Loading and indexing of the deprecation catalog.

The catalog is plain JSON on purpose. It is meant to be read, reviewed and
patched by hand, because the only thing that makes this tool worth anything is
whether the dates in it are true.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_CATALOG = Path(__file__).resolve().parent / "data" / "catalog.json"
ENV_CATALOG = "DEPDESK_CATALOG"


class CatalogError(Exception):
    """Raised when the catalog cannot be loaded or is internally inconsistent."""


def _parse_date(value: Optional[str], field_name: str, owner: str) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"{owner}: {field_name} is not an ISO date: {value!r}") from exc


@dataclass(frozen=True)
class Entry:
    """One deprecated thing: a model, an endpoint or a product."""

    id: str
    provider: str
    status: str
    kind: str = "model"
    deprecated_on: Optional[date] = None
    retires_on: Optional[date] = None
    earliest_retirement: Optional[date] = None
    replacement: Optional[str] = None
    note: Optional[str] = None
    retirement_note: Optional[str] = None

    def days_left(self, today: date) -> Optional[int]:
        """Days until the announced retirement. Negative once it has passed."""
        if self.retires_on is None:
            return None
        return (self.retires_on - today).days

    def days_until_earliest(self, today: date) -> Optional[int]:
        if self.earliest_retirement is None:
            return None
        return (self.earliest_retirement - today).days


@dataclass(frozen=True)
class ParameterRule:
    names: List[str]
    provider: str
    status: str
    behavior: str
    affects: List[str]
    affects_uncertain: List[str] = field(default_factory=list)
    scope_quote: Optional[str] = None
    sdk_note: Optional[str] = None
    replacement: Optional[str] = None
    uncertainty_note: Optional[str] = None


@dataclass
class Catalog:
    verified_on: date
    entries: List[Entry]
    parameters: List[ParameterRule]
    sources: List[Dict[str, Any]]
    policies: List[Dict[str, Any]]
    path: Path

    @property
    def by_id(self) -> Dict[str, Entry]:
        return {entry.id: entry for entry in self.entries}

    def models(self) -> List[Entry]:
        return [e for e in self.entries if e.kind == "model"]

    def age_days(self, today: date) -> int:
        return (today - self.verified_on).days

    def providers(self) -> List[str]:
        return sorted({e.provider for e in self.entries})


def _entries_from(raw: Dict[str, Any], key: str, kind: str) -> Iterable[Entry]:
    for item in raw.get(key, []):
        ident = item.get("id")
        if not ident:
            raise CatalogError(f"{key}: an entry has no id")
        status = item.get("status")
        if status not in {"active", "deprecated", "retired"}:
            raise CatalogError(f"{ident}: unknown status {status!r}")
        yield Entry(
            id=ident,
            provider=item.get("provider", "unknown"),
            status=status,
            kind=kind,
            deprecated_on=_parse_date(item.get("deprecated_on"), "deprecated_on", ident),
            retires_on=_parse_date(item.get("retires_on"), "retires_on", ident),
            earliest_retirement=_parse_date(
                item.get("earliest_retirement"), "earliest_retirement", ident
            ),
            replacement=item.get("replacement"),
            note=item.get("note"),
            retirement_note=item.get("retirement_note"),
        )


def resolve_catalog_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_CATALOG)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CATALOG


def load(path: Optional[str] = None) -> Catalog:
    resolved = resolve_catalog_path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"catalog is not valid JSON: {resolved}: {exc}") from exc

    if raw.get("schema") != 1:
        raise CatalogError(f"unsupported catalog schema: {raw.get('schema')!r}")

    entries: List[Entry] = []
    entries.extend(_entries_from(raw, "models", "model"))
    entries.extend(_entries_from(raw, "endpoints", "endpoint"))
    entries.extend(_entries_from(raw, "products", "product"))

    seen = set()
    for entry in entries:
        if entry.id in seen:
            raise CatalogError(f"duplicate id in catalog: {entry.id}")
        seen.add(entry.id)

    parameters = [
        ParameterRule(
            names=list(rule["names"]),
            provider=rule.get("provider", "unknown"),
            status=rule.get("status", "deprecated"),
            behavior=rule.get("behavior", ""),
            affects=list(rule.get("affects", [])),
            affects_uncertain=list(rule.get("affects_uncertain", [])),
            scope_quote=rule.get("scope_quote"),
            sdk_note=rule.get("sdk_note"),
            replacement=rule.get("replacement"),
            uncertainty_note=rule.get("uncertainty_note"),
        )
        for rule in raw.get("parameters", [])
    ]

    verified = _parse_date(raw.get("verified_on"), "verified_on", "catalog")
    if verified is None:
        raise CatalogError("catalog has no verified_on date")

    return Catalog(
        verified_on=verified,
        entries=entries,
        parameters=parameters,
        sources=list(raw.get("sources", [])),
        policies=list(raw.get("policies", [])),
        path=resolved,
    )
