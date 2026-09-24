"""Reading real call volume, so the report can say what share of traffic dies.

Accepts the CSV that Anthropic's Console exports (Usage, then Export), the
equivalent from other providers, or anything with a model column and a numeric
column. Column names are sniffed and can be overridden.
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MODEL_HINTS = ("model", "model_id", "modelid", "model name", "engine")
COUNT_HINTS = (
    "requests", "request_count", "calls", "count", "usage",
    "input_tokens", "tokens", "total_tokens", "total tokens", "n",
)
# Hints too short to be trusted inside a longer name. "n" used to match any
# column with the letter n in it, so api_key_name was summed as the count and
# a retired model carrying 90% of the traffic was reported at 0.0%.
EXACT_ONLY = {"n"}


class UsageError(Exception):
    """Raised when a usage file cannot be understood."""


def _normalise(column: str) -> str:
    return column.strip().lower().replace("-", "_")


def _ranked(header: Sequence[str], hints: Sequence[str], avoid: Optional[str] = None) -> List[str]:
    """Columns that match a hint, best first: exact names, then substrings."""
    normalised = {column: _normalise(column) for column in header if column != avoid}
    ranked: List[str] = []
    for hint in hints:
        for original, lowered in normalised.items():
            if lowered == hint and original not in ranked:
                ranked.append(original)
    for hint in hints:
        if hint in EXACT_ONLY:
            continue
        for original, lowered in normalised.items():
            if hint in lowered and original not in ranked:
                ranked.append(original)
    return ranked


def _pick(header: Sequence[str], hints: Sequence[str], avoid: Optional[str] = None) -> Optional[str]:
    ranked = _ranked(header, hints, avoid)
    return ranked[0] if ranked else None


def _pick_count(
    header: Sequence[str], rows: Sequence[Dict[str, Any]], avoid: Optional[str]
) -> Optional[str]:
    # A name is only a hint. usage_date_utc matched "usage" and was summed as
    # the count; the first candidate whose cells are actually numbers wins.
    for column in _ranked(header, COUNT_HINTS, avoid):
        cells = [row.get(column) for row in rows if row.get(column) not in (None, "")]
        if cells and sum(_coerce(cell) is not None for cell in cells) * 2 >= len(cells):
            return column
    return None


def _coerce(value: Any, european: bool = False) -> Optional[float]:
    """A finite, non-negative number, or None when the cell is not one.

    A semicolon export is European Excel, where 1.420 is one thousand four
    hundred and 0,50 is a half. Read the English way, a count of 1.420 became
    1.42 and threw that model's share off by a factor of a thousand.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        cleaned = str(value).strip().replace("_", "").replace(" ", "")
        if european:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
        if not cleaned:
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
    # NaN, infinity and negative counts turned every share into nan or into
    # percentages above 100, and NaN made the JSON output invalid.
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _header(original: Sequence[str], wanted: Optional[str], kind: str, path: Path) -> Optional[str]:
    if wanted is None:
        return None
    for column in original:
        if column == wanted or _normalise(column) == _normalise(wanted):
            return column
    # A mistyped override silently summed zeros in 0.2.0.
    raise UsageError(
        f"{path}: there is no {kind} column called {wanted!r}. "
        f"Columns present: {', '.join(original)}."
    )


def load_usage(
    path: Path,
    model_column: Optional[str] = None,
    count_column: Optional[str] = None,
) -> Tuple[Dict[str, float], str]:
    """Return {model: total} plus the name of the column that was summed."""
    try:
        # utf-8-sig: Excel writes a byte order mark, which glued itself to the
        # first column name and made --usage-model-column model miss it.
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read usage file: {path}: {exc}") from exc

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return _load_json(stripped, path, model_column, count_column)

    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise UsageError(f"usage file has no header row: {path}")
    # Excel in most of Europe exports with semicolons. Read as commas, every
    # row became one long model name and the real model got no share at all.
    try:
        delimiter = csv.Sniffer().sniff(lines[0], delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(lines, delimiter=delimiter)
    header = [column for column in (reader.fieldnames or []) if column is not None]
    if not header:
        raise UsageError(f"usage file has no header row: {path}")

    model_col = _header(header, model_column, "model", path) or _pick(header, MODEL_HINTS)
    if model_col is None:
        raise UsageError(
            f"could not find a model column in {path}. "
            f"Columns present: {', '.join(header)}. "
            f"Pass --usage-model-column to say which one it is."
        )
    rows = list(reader)
    # Never the model column itself: model_version used to be picked as both.
    count_col = _header(header, count_column, "count", path) or _pick_count(header, rows, model_col)

    totals: Dict[str, float] = defaultdict(float)
    for row in rows:
        model = (row.get(model_col) or "").strip()
        if not model:
            continue
        if count_col is None:
            totals[model] += 1.0
            continue
        number = _coerce(row.get(count_col), european=delimiter == ";")
        totals[model] += number if number is not None else 0.0

    if not totals:
        raise UsageError(f"usage file produced no rows: {path}")
    if count_col is not None and sum(totals.values()) == 0:
        # Every share would be 0.0%, which reads as "nobody calls this".
        raise UsageError(
            f"{path}: the column {count_col!r} holds no positive numbers. "
            "Pass --usage-count-column to say which column to sum."
        )
    return dict(totals), count_col or "rows"


def _load_json(
    text: str,
    path: Path,
    model_column: Optional[str] = None,
    count_column: Optional[str] = None,
) -> Tuple[Dict[str, float], str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"usage file is not valid JSON: {path}: {exc}") from exc

    # {"data": [...], "has_more": false} is how most usage APIs wrap the rows,
    # and it used to fail with "value for 'data' is not a number".
    if isinstance(payload, dict):
        rows = payload.get("data")
        if rows is None and len(payload) == 1:
            rows = next(iter(payload.values()))
        if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
            payload = rows

    totals: Dict[str, float] = defaultdict(float)
    if isinstance(payload, dict):
        for model, value in payload.items():
            if isinstance(value, (dict, list)):
                raise UsageError(f"{path}: value for {model!r} is not a number")
            # NaN or a negative count is skipped, as it is in a CSV.
            number = _coerce(value)
            totals[str(model)] += number if number is not None else 0.0
        if not totals or sum(totals.values()) == 0:
            raise UsageError(f"{path}: could not find model counts in this JSON")
        return dict(totals), "value"

    if isinstance(payload, list):
        items: List[Dict[str, Any]] = [item for item in payload if isinstance(item, dict)]
        if len(items) != len(payload):
            raise UsageError(f"{path}: expected a list of objects")
        keys = sorted({key for item in items for key in item})
        model_key = _header(keys, model_column, "model", path) or _pick(keys, MODEL_HINTS)
        if model_key is None:
            raise UsageError(f"{path}: could not find a model key in this JSON")
        # The CSV rules apply to the keys too. Only requests, count and tokens
        # were read before, and input_tokens was quietly counted as one per row.
        count_key = _header(keys, count_column, "count", path) or _pick_count(keys, items, model_key)
        for item in items:
            model = item.get(model_key)
            if not model:
                continue
            if count_key is None:
                totals[str(model)] += 1.0
                continue
            number = _coerce(item.get(count_key))
            totals[str(model)] += number if number is not None else 0.0
        if totals and (count_key is None or sum(totals.values()) > 0):
            return dict(totals), count_key or "rows"

    raise UsageError(f"{path}: could not find model counts in this JSON")


def share(totals: Dict[str, float]) -> Dict[str, float]:
    grand = sum(totals.values())
    if grand <= 0:
        return {model: 0.0 for model in totals}
    return {model: value / grand for model, value in totals.items()}


def top(totals: Dict[str, float], limit: int = 10) -> List[Tuple[str, float]]:
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
