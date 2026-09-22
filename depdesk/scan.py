"""Finding model identifiers, and deprecated parameters, in a source tree."""

from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set

from .catalog import Catalog, ParameterRule

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".tox", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next",
    "dist", "build", "target", "vendor", ".terraform", "site-packages",
    ".idea", ".vscode", "coverage", ".cache",
}

# Extensionless files worth reading anyway: config and env live there.
ALLOWED_NAMES = {
    "Dockerfile", "Makefile", "Procfile", "docker-compose.yml",
    "docker-compose.yaml", ".env", ".env.example", ".env.sample",
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tar", ".bz2", ".xz", ".7z", ".mp3", ".mp4", ".mov", ".wav",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".so", ".dylib", ".dll",
    ".pyc", ".pyo", ".class", ".jar", ".wasm", ".lock", ".bin", ".db",
    ".sqlite", ".sqlite3", ".parquet",
}

MAX_BYTES = 2_000_000

# A line carrying this marker is not scanned. Documentation, changelogs and
# migration notes legitimately name models that are dead, and a tool you
# cannot silence on a known-good line is a tool people stop running.
IGNORE_PRAGMA = "depdesk: ignore"

# Identifiers that look like a hosted model but are not in the catalog. Kept
# deliberately loose: a false "unknown" is cheap, a missed one is not.
UNKNOWN_PATTERNS = [
    re.compile(r"\bclaude-[a-z0-9][a-z0-9.\-]{2,}", re.IGNORECASE),
    re.compile(r"\bgpt-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
    re.compile(r"\bo[1-9]-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
    re.compile(r"\b(?:gemini|mistral|llama|grok|command|deepseek)-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
]


@dataclass(frozen=True)
class Hit:
    """One occurrence of a catalog identifier in a file."""

    identifier: str
    path: Path
    line: int
    excerpt: str


@dataclass(frozen=True)
class ParamHit:
    """A deprecated parameter passed in a file that also names an affected model."""

    parameter: str
    path: Path
    line: int
    excerpt: str
    models_in_file: List[str]
    certain: bool


@dataclass
class ScanResult:
    hits: List[Hit]
    param_hits: List[ParamHit]
    unknown: Dict[str, List[Hit]]
    files_scanned: int
    files_skipped: int

    def identifiers(self) -> Set[str]:
        return {hit.identifier for hit in self.hits}

    def by_identifier(self) -> Dict[str, List[Hit]]:
        grouped: Dict[str, List[Hit]] = {}
        for hit in self.hits:
            grouped.setdefault(hit.identifier, []).append(hit)
        return grouped


def _is_scannable(path: Path) -> bool:
    if path.name in ALLOWED_NAMES:
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.suffix == "":
        return False
    return True


def _matches_any(path: Path, patterns: Sequence[str]) -> bool:
    """Glob match against the path as written, its absolute form and its name."""
    candidates = (str(path), str(path.resolve()), path.name)
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
    )


def iter_files(
    roots: Sequence[Path],
    exclude: Optional[Set[Path]] = None,
    exclude_globs: Optional[Sequence[str]] = None,
) -> Iterator[Path]:
    excluded = {p.resolve() for p in (exclude or set())}
    globs = tuple(exclude_globs or ())
    for root in roots:
        if root.is_file():
            if root.resolve() not in excluded and not _matches_any(root, globs):
                yield root
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.resolve() in excluded:
                continue
            if globs and _matches_any(path, globs):
                continue
            if _is_scannable(path):
                yield path


def _build_id_pattern(identifiers: Iterable[str]) -> Optional[re.Pattern]:
    # Longest first so that gpt-4-turbo wins over any shorter prefix.  # depdesk: ignore
    ordered = sorted({i for i in identifiers}, key=len, reverse=True)
    if not ordered:
        return None
    alternation = "|".join(re.escape(i) for i in ordered)
    # A model id may not be glued to another identifier character. This is what
    # stops gpt-4 from matching inside gpt-4o.  # depdesk: ignore
    return re.compile(r"(?<![A-Za-z0-9_.\-])(" + alternation + r")(?![A-Za-z0-9_.\-])")


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _excerpt(line: str, limit: int = 160) -> str:
    trimmed = line.strip()
    return trimmed if len(trimmed) <= limit else trimmed[: limit - 1] + "…"


def _find_params_python(text: str, path: Path, names: Set[str]) -> List[tuple]:
    """Locate deprecated keyword arguments precisely, using the stdlib parser."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in names:
                found.append((keyword.arg, getattr(keyword.value, "lineno", node.lineno)))
    return found


def _find_params_text(text: str, names: Set[str]) -> List[tuple]:
    """Fallback for non-Python files: a keyword followed by an assignment."""
    found = []
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in sorted(names)) + r")\s*[:=]\s*[^=\s]"
    )
    for index, line in enumerate(text.splitlines(), start=1):
        for match in pattern.finditer(line):
            found.append((match.group(1), index))
    return found


def scan(
    roots: Sequence[Path],
    catalog: Catalog,
    exclude: Optional[Set[Path]] = None,
    exclude_globs: Optional[Sequence[str]] = None,
) -> ScanResult:
    known_ids = {entry.id for entry in catalog.entries}
    id_pattern = _build_id_pattern(known_ids)

    param_names: Set[str] = set()
    certain_models: Set[str] = set()
    uncertain_models: Set[str] = set()
    for rule in catalog.parameters:
        param_names.update(rule.names)
        certain_models.update(rule.affects)
        uncertain_models.update(rule.affects_uncertain)

    hits: List[Hit] = []
    param_hits: List[ParamHit] = []
    unknown: Dict[str, List[Hit]] = {}
    scanned = 0
    skipped = 0

    for path in iter_files(roots, exclude, exclude_globs):
        text = _read(path)
        if text is None:
            skipped += 1
            continue
        scanned += 1
        lines = text.splitlines()

        file_ids: Set[str] = set()
        if id_pattern is not None:
            for index, line in enumerate(lines, start=1):
                if IGNORE_PRAGMA in line:
                    continue
                for match in id_pattern.finditer(line):
                    identifier = match.group(1)
                    file_ids.add(identifier)
                    hits.append(Hit(identifier, path, index, _excerpt(line)))

        # Identifiers that look like models but are not in the catalog. We
        # cannot tell the user anything about these, and saying so is the point.
        for index, line in enumerate(lines, start=1):
            if IGNORE_PRAGMA in line:
                continue
            for pattern in UNKNOWN_PATTERNS:
                for match in pattern.finditer(line):
                    candidate = match.group(0).rstrip(".-")
                    if candidate in known_ids:
                        continue
                    # There is deliberately no "is a piece of a known id" guard
                    # here: the regex is greedy and already captures the whole
                    # token, and the old guard hid gpt-4o just for being a  # depdesk: ignore
                    # prefix of gpt-4o-audio. Silencing a real finding beats  # depdesk: ignore
                    # no noise.
                    unknown.setdefault(candidate, []).append(
                        Hit(candidate, path, index, _excerpt(line))
                    )

        if param_names:
            affected_certain = sorted(file_ids & certain_models)
            affected_uncertain = sorted(file_ids & uncertain_models)
            if affected_certain or affected_uncertain:
                if path.suffix == ".py":
                    occurrences = _find_params_python(text, path, param_names)
                else:
                    occurrences = _find_params_text(text, param_names)
                for name, line_no in occurrences:
                    line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
                    if IGNORE_PRAGMA in line_text:
                        continue
                    param_hits.append(
                        ParamHit(
                            parameter=name,
                            path=path,
                            line=line_no,
                            excerpt=_excerpt(line_text),
                            models_in_file=affected_certain or affected_uncertain,
                            certain=bool(affected_certain),
                        )
                    )

    return ScanResult(
        hits=hits,
        param_hits=param_hits,
        unknown=unknown,
        files_scanned=scanned,
        files_skipped=skipped,
    )


def rule_for(catalog: Catalog, parameter: str) -> Optional[ParameterRule]:
    for rule in catalog.parameters:
        if parameter in rule.names:
            return rule
    return None
