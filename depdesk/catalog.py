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
    # The page an entry was transcribed from, when it is not the provider's
    # deprecation page (an alias listed only on the models overview).
    source: Optional[str] = None

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


def _list_of_objects(raw: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """The list under `key`, refusing anything else.

    A catalog passed with --catalog is somebody's hand edit. In 0.2.0 a list
    where an object belonged, or a missing key, ended in a traceback and exit
    1, which the contract reserves for warnings under --strict.
    """
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CatalogError(f"{key}: expected a list of objects")
    return value


def _text(item: Dict[str, Any], key: str, owner: str) -> Optional[str]:
    value = item.get(key)
    if value is not None and not isinstance(value, str):
        raise CatalogError(f"{owner}: {key} must be a string")
    return value


def _sources(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    sources = _list_of_objects(raw, "sources")
    for source in sources:
        between = source.get("between")
        if between is None:
            continue
        # A hand-edited marker pair that was not a pair crashed upstream with
        # exit 1, which the drift workflow reads as "the page moved".
        if (
            not isinstance(between, list) or len(between) != 2
            or not all(isinstance(m, str) and m for m in between)
        ):
            raise CatalogError(f"sources: between must be two non-empty strings, got {between!r}")
    return sources


def _entries_from(raw: Dict[str, Any], key: str, kind: str) -> Iterable[Entry]:
    for item in _list_of_objects(raw, key):
        ident = item.get("id")
        if not ident or not isinstance(ident, str):
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
            replacement=_text(item, "replacement", ident),
            note=_text(item, "note", ident),
            retirement_note=_text(item, "retirement_note", ident),
            source=_text(item, "source", ident),
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
        raw = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found: {resolved}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"catalog is not valid JSON: {resolved}: {exc}") from exc
    except OSError as exc:
        raise CatalogError(f"catalog cannot be read: {resolved}: {exc.strerror or exc}") from exc

    if not isinstance(raw, dict):
        raise CatalogError(f"catalog is not a JSON object: {resolved}")
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

    rules = _list_of_objects(raw, "parameters")
    for rule in rules:
        names = rule.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise CatalogError("parameters: every rule needs a list of names")
        for key in ("affects", "affects_uncertain"):
            if not isinstance(rule.get(key, []), list):
                raise CatalogError(f"parameters: {key} must be a list")

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
        for rule in rules
    ]

    verified = _parse_date(raw.get("verified_on"), "verified_on", "catalog")
    if verified is None:
        raise CatalogError("catalog has no verified_on date")

    return Catalog(
        verified_on=verified,
        entries=entries,
        parameters=parameters,
        sources=_sources(raw),
        policies=_list_of_objects(raw, "policies"),
        path=resolved,
    )
