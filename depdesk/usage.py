"""Reading real call volume, so the report can say what share of traffic dies.

Accepts the CSV that Anthropic's Console exports (Usage, then Export), the
equivalent from other providers, or anything with a model column and a numeric
column. Column names are sniffed and can be overridden.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MODEL_HINTS = ("model", "model_id", "modelid", "model name", "engine")
COUNT_HINTS = (
    "requests", "request_count", "calls", "count", "n", "usage",
    "input_tokens", "tokens", "total_tokens", "total tokens",
)


class UsageError(Exception):
    """Raised when a usage file cannot be understood."""


def _pick(header: Sequence[str], hints: Sequence[str]) -> Optional[str]:
    normalised = {column: column.strip().lower().replace("-", "_") for column in header}
    for hint in hints:
        for original, lowered in normalised.items():
            if lowered == hint:
                return original
    for hint in hints:
        for original, lowered in normalised.items():
            if hint in lowered:
                return original
    return None


def _coerce(value: str) -> float:
    cleaned = value.strip().replace(",", "").replace("_", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def load_usage(
    path: Path,
    model_column: Optional[str] = None,
    count_column: Optional[str] = None,
) -> Tuple[Dict[str, float], str]:
    """Return {model: total} plus the name of the column that was summed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read usage file: {path}: {exc}") from exc

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return _load_json(stripped, path)

    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise UsageError(f"usage file has no header row: {path}")

    model_col = model_column or _pick(reader.fieldnames, MODEL_HINTS)
    if model_col is None:
        raise UsageError(
            f"could not find a model column in {path}. "
            f"Columns present: {', '.join(reader.fieldnames)}. "
            f"Pass --usage-model-column to say which one it is."
        )
    count_col = count_column or _pick(reader.fieldnames, COUNT_HINTS)

    totals: Dict[str, float] = defaultdict(float)
    for row in reader:
        model = (row.get(model_col) or "").strip()
        if not model:
            continue
        totals[model] += _coerce(row.get(count_col, "")) if count_col else 1.0

    if not totals:
        raise UsageError(f"usage file produced no rows: {path}")
    return dict(totals), count_col or "rows"


def _load_json(text: str, path: Path) -> Tuple[Dict[str, float], str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"usage file is not valid JSON: {path}: {exc}") from exc

    totals: Dict[str, float] = defaultdict(float)
    if isinstance(payload, dict):
        for model, value in payload.items():
            try:
                totals[str(model)] += float(value)
            except (TypeError, ValueError):
                raise UsageError(f"{path}: value for {model!r} is not a number") from None
        return dict(totals), "value"

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raise UsageError(f"{path}: expected a list of objects")
            model = item.get("model") or item.get("model_id")
            if not model:
                continue
            amount = item.get("requests", item.get("count", item.get("tokens", 1)))
            try:
                totals[str(model)] += float(amount)
            except (TypeError, ValueError):
                totals[str(model)] += 1.0
        if totals:
            return dict(totals), "requests"

    raise UsageError(f"{path}: could not find model counts in this JSON")


def share(totals: Dict[str, float]) -> Dict[str, float]:
    grand = sum(totals.values())
    if grand <= 0:
        return {model: 0.0 for model in totals}
    return {model: value / grand for model, value in totals.items()}


def top(totals: Dict[str, float], limit: int = 10) -> List[Tuple[str, float]]:
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]
