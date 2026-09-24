"""Finding model identifiers, and deprecated parameters, in a source tree."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .catalog import Catalog, ParameterRule

# `env` is not here on purpose. As a virtualenv name it is covered by the
# pyvenv.cfg check in iter_files, and as a plain name it is where deployment
# config lives: deploy/env/production.env was being skipped with everything
# in it.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".tox", ".venv", "venv", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next",
    "dist", "build", "target", "vendor", ".terraform", "site-packages",
    ".idea", ".vscode", "coverage", ".cache",
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tar", ".bz2", ".xz", ".7z", ".mp3", ".mp4", ".mov", ".wav",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".so", ".dylib", ".dll",
    ".pyc", ".pyo", ".class", ".jar", ".wasm", ".lock", ".bin", ".db",
    ".sqlite", ".sqlite3", ".parquet",
    # Model weights and packaged binaries. Checked into a repository or built
    # into it before the CI step, they are large, and without this they turned
    # into a "could not read" warning that failed --strict.
    ".onnx", ".safetensors", ".gguf", ".pt", ".pth", ".ckpt", ".h5", ".pkl",
    ".npy", ".npz", ".whl", ".egg", ".exe", ".msi", ".dmg", ".iso", ".img",
    ".o", ".a", ".lib", ".obj", ".webm", ".mkv", ".avi", ".flac", ".ogg",
}

# Text files that may legitimately carry a NUL (a separator constant, a test
# fixture). Anything else with a NUL near the start is treated as binary.
TEXT_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".env", ".md", ".txt", ".rst", ".sh", ".bash",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".c", ".h",
    ".cpp", ".hpp", ".sql", ".tf", ".ipynb", ".html", ".xml", ".csv",
}

# Up to 0.2.0 anything over 2 MB was dropped without a word. A prompt file or
# a bundled config can be that large, and the id on its first line went
# unreported. Files over this size are still skipped, but they are listed.
MAX_BYTES = 20_000_000

# The ast module recurses, and a generated file with a very deep expression
# took the interpreter down with it: a segfault on 3.9, from a single line of
# 200 thousand additions. Past either limit the text rules are used for
# parameters instead. Hand-written Python has neither.
AST_MAX_CHARS = 1_000_000
AST_MAX_LINE = 10_000

# str.splitlines also breaks on form feed and a handful of other separators
# that the ast module does not count, so after a ^L every parameter finding
# pointed one line too far, and the ignore pragma was read from the wrong
# line. Lines are split the way the parser splits them.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

# A line carrying `depdesk: ignore` is not scanned. Documentation, changelogs
# and migration notes legitimately name models that are dead, and a tool you
# cannot silence on a known-good line is a tool people stop running.
#
# `depdesk: ignore until=2026-12-01` silences the line only until that date.
# Anything else written after the marker that mentions `until`, or anything
# glued to it (ignored, ignore-until), silences nothing: in 0.2.0 a typo such
# as `until 2026-01-01` or `until:2026-01-01` fell through to the permanent
# form, which is the exact outcome the date exists to prevent.
_PRAGMA = re.compile(r"depdesk:\s*ignore(?![\w-])(.*)")
_UNTIL_WORD = re.compile(r"\s*until\b", re.IGNORECASE)
_UNTIL = re.compile(r"\s+until=(\d{4}-\d{2}-\d{2})(?!\d)", re.IGNORECASE)


def is_ignored(line: str, today: date) -> bool:
    """Whether the pragma on this line still silences it on `today`.

    A date that is not a real date, or an `until` written any other way, does
    not silence anything. The line gets scanned, and the pragma shows up in the
    excerpt next to the finding it failed to hide.
    """
    match = _PRAGMA.search(line)
    if match is None:
        return False
    rest = match.group(1)
    if not _UNTIL_WORD.match(rest):
        return True
    dated = _UNTIL.match(rest)
    if dated is None:
        return False
    try:
        until = date.fromisoformat(dated.group(1))
    except ValueError:
        return False
    return today < until


# Identifiers that look like a hosted model but are not in the catalog. Kept
# deliberately loose: a false "unknown" is cheap, a missed one is not.
UNKNOWN_PATTERNS = [
    re.compile(r"\bclaude-[a-z0-9][a-z0-9.\-]{2,}", re.IGNORECASE),
    re.compile(r"\bgpt-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
    re.compile(r"\bo[1-9]-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
    re.compile(r"\b(?:text|code)-(?:davinci|curie|babbage|ada|cushman)-[a-z0-9.\-]+", re.IGNORECASE),
    re.compile(r"\b(?:gemini|mistral|llama|grok|command|deepseek)-[a-z0-9][a-z0-9.\-]{1,}", re.IGNORECASE),
]

# What may not touch an id on either side. A dot is allowed after the id when
# it ends a sentence: "still calls claude-3-opus-20240229." was silent in  # depdesk: ignore
# 0.2.0. Only a sentence end, though: a dot before a quote or a star is a
# prefix check, startswith("gpt-4.") or "gpt-4.*", and not a use of gpt-4.  # depdesk: ignore
_BEFORE = r"(?<![A-Za-z0-9_.\-])"
_AFTER = r"(?![A-Za-z0-9_\-]|\.(?!\s|$|[)\]]))"

_QUOTES = "\"'`"
# An id that could also be a variable name. Products such as "Agent Builder"  # depdesk: ignore
# have a space and are matched in prose on purpose; the first version of the
# bare id rule caught them too and stopped finding them.
_BARE_WORD = re.compile(r"[A-Za-z0-9_.]+")
_CONFIG_LINE = re.compile(r"\s*(?:export\s+)?[A-Za-z_][\w.\-]*\s*[:=]\s*[\"']?$")


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
    # A catalog id glued to a prefix, such as anthropic.claude-2.0, keyed by  # depdesk: ignore
    # the whole token and pointing at the id it contains.
    embedded: Dict[str, str] = field(default_factory=dict)
    # Files that could not be read, with the reason. They used to vanish into
    # a counter nobody printed, so a tree with one unreadable file among many
    # still read as clean.
    skipped: List[Tuple[Path, str]] = field(default_factory=list)

    def identifiers(self) -> Set[str]:
        return {hit.identifier for hit in self.hits}

    def by_identifier(self) -> Dict[str, List[Hit]]:
        grouped: Dict[str, List[Hit]] = {}
        for hit in self.hits:
            grouped.setdefault(hit.identifier, []).append(hit)
        return grouped


def _is_scannable(path: Path) -> bool:
    # Extensionless files are read too: .envrc, Jenkinsfile, Containerfile and
    # bin/ scripts carry model names, and pre-commit was already passing them
    # explicitly while the tree walk in CI dropped them. Binaries without an
    # extension are recognised when read, by the NUL bytes they contain.
    return path.suffix.lower() not in SKIP_SUFFIXES


def _globs_match(candidates: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in patterns
        for candidate in candidates
    )


def _candidates(path: Path, root: Optional[Path]) -> List[str]:
    """What an --exclude glob is matched against.

    Never the absolute path: a checkout at .../docs/docs, which is what GitHub
    Actions makes of a repository called docs, lost every file to
    `--exclude "*/docs/*"` because the glob matched the directories above it.
    """
    names = [path.name]

    def add(value: str) -> None:
        names.append(value)
        if value.startswith(("/", "../")):
            return
        names.append("./" + value)
        # Every tail of a relative path too, the way .gitignore reads a
        # pattern, so `generated/*` means the same in a tree walk and for the
        # files pre-commit passes one by one.
        parts = value.split("/")
        names.extend("/".join(parts[i:]) for i in range(1, len(parts)))

    # As written, which is how 0.2.0 matched `--exclude "src/generated/*"` with
    # `check src` and how pre-commit passes files, and relative to where the
    # command runs. Both stay below the working directory, so the directories
    # above a checkout never take part.
    add(path.as_posix())
    try:
        add(path.resolve().relative_to(Path.cwd().resolve()).as_posix())
    except ValueError:
        pass
    if root is not None:
        add(path.relative_to(root).as_posix())
    return names


def iter_files(
    roots: Sequence[Path],
    exclude: Optional[Set[Path]] = None,
    exclude_globs: Optional[Sequence[str]] = None,
    blocked: Optional[List[Path]] = None,
) -> Iterator[Path]:
    blocked = blocked if blocked is not None else []
    excluded = {p.resolve() for p in (exclude or set())}
    globs = tuple(exclude_globs or ())
    # Overlapping roots, `check . app/main.py`, used to scan the same file
    # twice and print every location twice.
    seen: Set[Path] = set()

    def fresh(path: Path) -> bool:
        resolved = path.resolve()
        if resolved in seen or resolved in excluded:
            return False
        seen.add(resolved)
        return True

    def is_venv(directory: Path) -> bool:
        try:
            return (directory / "pyvenv.cfg").exists()
        except OSError:
            return False

    for root in roots:
        if root.is_file():
            # A file the user named explicitly, which is also what pre-commit
            # does with every staged file, is read whatever its directory is
            # called.
            if not _globs_match(_candidates(root, None), globs) and fresh(root):
                yield root
            continue
        # SKIP_DIRS applies only below the root the user named. Up to 0.2.0 it
        # was checked against every part of the path, so `depdesk check
        # /build/app`, or a checkout under a directory called dist, read zero
        # files and still printed "Nothing deprecated found". Pruning during
        # the walk also stops it from listing all of node_modules just to throw
        # every entry away.
        # A directory that cannot be listed (a database volume owned by
        # another user, mode 700) crashed the walk with a traceback. It is
        # reported as unread instead.
        def unreadable(error: OSError) -> None:
            if error.filename:
                blocked.append(Path(error.filename))

        for directory, subdirs, files in os.walk(root, onerror=unreadable):
            base = Path(directory)
            subdirs[:] = sorted(d for d in subdirs if d not in SKIP_DIRS and not is_venv(base / d))
            for name in sorted(files):
                path = base / name
                if path.is_symlink() or not path.is_file():
                    continue
                if not _is_scannable(path):
                    continue
                if globs and _globs_match(_candidates(path, root), globs):
                    continue
                if fresh(path):
                    yield path


def _split_ids(identifiers: Iterable[str]) -> Tuple[List[str], List[str], List[str]]:
    """Plain ids, API paths (/v1/prompts) and headers (OpenAI-Beta: x=v1)."""  # depdesk: ignore
    plain, paths, headers = [], [], []
    for ident in sorted(set(identifiers), key=len, reverse=True):
        if ident.startswith("/"):
            paths.append(ident)
        elif ": " in ident:
            headers.append(ident)
        else:
            plain.append(ident)
    return plain, paths, headers


def _alternation(identifiers: Sequence[str]) -> str:
    # Longest first so that gpt-4-turbo wins over any shorter prefix.  # depdesk: ignore
    return "|".join(re.escape(i) for i in identifiers)


def _build_id_pattern(identifiers: Iterable[str]) -> Optional[re.Pattern]:
    plain, _, _ = _split_ids(identifiers)
    if not plain:
        return None
    # A model id may not be glued to another identifier character. This is what
    # stops gpt-4 from matching inside gpt-4o.  # depdesk: ignore
    return re.compile(_BEFORE + "(" + _alternation(plain) + ")" + _AFTER)


def _build_path_pattern(identifiers: Iterable[str]) -> Optional[re.Pattern]:
    # An endpoint is written after a host, https://api.openai.com/v1/prompts,  # depdesk: ignore
    # so the left boundary of a model id would never let it match.
    _, paths, _ = _split_ids(identifiers)
    if not paths:
        return None
    return re.compile("(" + _alternation(paths) + r")(?![A-Za-z0-9_.\-])")


def _build_header_patterns(identifiers: Iterable[str]) -> List[Tuple[re.Pattern, str]]:
    # Code never contains the header as the catalog spells it: it writes
    # {"OpenAI-Beta": "assistants=v1"}, or a tuple, or a curl -H string.  # depdesk: ignore
    _, _, headers = _split_ids(identifiers)
    patterns = []
    for ident in headers:
        name, value = ident.split(": ", 1)
        patterns.append((
            re.compile(
                re.escape(name) + r"[\"']?\s*\]?\s*[:,=]\s*[\"']?" + re.escape(value) + r"(?![A-Za-z0-9_.\-])",
                re.IGNORECASE,
            ),
            ident,
        ))
    return patterns


def _build_embedded_pattern(identifiers: Iterable[str]) -> Optional[re.Pattern]:
    """A catalog id right after a `.` or `-`, which the id pattern refuses.

    anthropic.claude-2.0 and prod-gpt-4 matched nothing at all: the id pass  # depdesk: ignore
    rejects anything glued on the left, and the unknown pass skipped them for
    being catalog ids. They are not the catalog entry either, because a cloud
    platform id follows the platform's own retirement schedule, so they are
    reported as unknown instead of being given a date that may be wrong. Only
    hyphenated ids, and only `.` and `-` as glue: anything looser makes every
    word ending in o1 look like a model.  # depdesk: ignore
    """
    plain, _, _ = _split_ids(identifiers)
    plain = [i for i in plain if "-" in i]
    if not plain:
        return None
    return re.compile(r"(?<=[.\-])(" + _alternation(plain) + ")" + _AFTER)


_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_.\-]")


def _token_start(line: str, index: int) -> int:
    while index > 0 and _TOKEN_CHAR.match(line[index - 1]):
        index -= 1
    return index


_MODEL_KEY = re.compile(
    r"(?i)(?:^|[\s{,\[(\-])[\"']?[\w.\-]*(?:model|engine|deployment)[\w.\-]*[\"']?\s*[:=]\s*[\"']?\[?[\w\s,.\-\"']*$"
)
_MODEL_FLAG = re.compile(r"(?i)--(?:model|engine)[=\s]\s*[\"']?$")
_LIST_ITEM = re.compile(r"\s*-\s*[\"']?$")
_CODE_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".rb"}


def _plausible_bare_id(line: str, start: int, end: int, suffix: str = "") -> bool:
    """A catalog id with no hyphen, o1, counts only where a model name would be.  # depdesk: ignore

    Once o1 entered the catalog, `def overlap(o1, o2)` failed the build as a  # depdesk: ignore
    model retiring in 29 days. A bare id counts when it is quoted, when it is
    the value of a key or flag that names a model (MODEL=o1, ENV MODEL=o1,  # depdesk: ignore
    model: [o1, gpt-4o], --model o1), or, outside code files, when it is the  # depdesk: ignore
    whole value of a config line or a YAML list item.
    """
    before = line[start - 1] if start > 0 else ""
    after = line[end] if end < len(line) else ""
    if before and before in _QUOTES and after == before:
        return True
    prefix = line[:start]
    if _MODEL_KEY.search(prefix) or _MODEL_FLAG.search(prefix):
        return True
    if suffix in _CODE_SUFFIXES:
        return False
    tail = line[end:].strip().strip("\"'").strip()
    if tail not in ("", ",") and not tail.startswith("#"):
        return False
    return bool(_CONFIG_LINE.fullmatch(prefix) or _LIST_ITEM.fullmatch(prefix))


def _read(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """The file as text, or None and why not. A binary file is None, None."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
        textual = path.suffix.lower() in TEXT_SUFFIXES
        utf16 = head.startswith((b"\xff\xfe", b"\xfe\xff"))
        # The NUL check comes before the size check: a 30 MB model file is a
        # binary, not a text file that could not be read.
        if b"\x00" in head and not utf16 and not textual:
            return None, None
        if path.stat().st_size > MAX_BYTES:
            return None, f"larger than {MAX_BYTES // 1_000_000} MB"
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"could not be read ({exc.strerror or exc.__class__.__name__})"
    if utf16:
        return raw.decode("utf-16", errors="replace"), None
    # Model ids are ASCII, so a Latin-1 .env or a cp1252 comment must not cost
    # the whole file: 0.2.0 dropped any file that was not valid UTF-8, and the
    # retired model in it with it.
    return raw.decode("utf-8-sig", errors="replace"), None


def _looks_like_a_catalog(text: str) -> bool:
    """A catalog is a list of dead models by definition, so reporting it is noise.

    The catalog in use is already excluded by path, but a copy of it sitting in
    the tree is not: a fork, a vendored file, or this repository scanned while
    the CLI runs from an installed wheel. That last one made the same command
    pass from source and fail from PyPI, which is the kind of difference that
    makes people stop trusting the output.
    """
    if '"verified_on"' not in text or '"models"' not in text:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and "schema" in data and isinstance(data.get("models"), list)


def _excerpt(line: str, at: int = 0, limit: int = 160) -> str:
    # A window around the match, not the start of the line: on a minified
    # bundle the start says nothing, and stripping a 2 MB line once per hit
    # was most of the run time.
    if len(line) <= limit * 2:
        trimmed = line.strip()
        if len(trimmed) <= limit:
            return trimmed
    start = max(0, at - limit // 3)
    window = line[start:start + limit].strip()
    return ("…" if start > 0 else "") + window + "…"


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _param_spellings(names: Set[str]) -> Dict[str, str]:
    # The JavaScript SDKs take topP and topK and send top_p and top_k.
    spellings = {}
    for name in names:
        spellings[name] = name
        spellings[_camel(name)] = name
    return spellings


def _find_params_python(
    text: str, lines: Sequence[str], spellings: Dict[str, str]
) -> Optional[List[tuple]]:
    """Deprecated arguments located with the stdlib parser, or None if it cannot.

    Keyword arguments, dict literals and item assignment, because
    `create(**params)` and `kwargs["top_p"] = 0.9` send the same request as a
    keyword and were invisible in 0.2.0.
    """
    if len(text) > AST_MAX_CHARS or any(len(line) > AST_MAX_LINE for line in lines):
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # A file this interpreter cannot parse (a newer syntax, a stray NUL)
        # still gets the text rules instead of nothing at all.
        return None
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in spellings:
                    found.append((spellings[keyword.arg], getattr(keyword.value, "lineno", node.lineno),
                                  keyword.arg != spellings[keyword.arg]))
        elif isinstance(node, ast.Dict):
            # Only a dict that is a request, one with a "model" key. A tool
            # schema's {"temperature": {"type": "number"}} is not.
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "model" not in keys:
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value in spellings:
                    found.append((spellings[key.value], key.lineno, key.value != spellings[key.value]))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            # kwargs["top_p"] = 0.9 is often a request and sometimes a reading
            # from a sensor: reported for review, never as a failure.
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in spellings:
                found.append((spellings[key.value], node.lineno, True))
    return found


def _find_params_text(lines: Sequence[str], spellings: Dict[str, str]) -> List[tuple]:
    """For everything that is not Python: a name followed by an assignment.

    The quotes are optional on both sides, because JSON bodies, TypeScript and
    curl payloads write "temperature": 0.2, which the 0.2.0 rule never matched.
    """
    alternation = "|".join(re.escape(n) for n in sorted(spellings, key=len, reverse=True))
    # Not followed by { or [: "temperature": {"type": "number"} is a schema.
    pattern = re.compile(r"(?<![A-Za-z0-9_])[\"']?(" + alternation + r")[\"']?\s*[:=]\s*[^=\s{\[]")
    found = []
    for index, line in enumerate(lines, start=1):
        for match in pattern.finditer(line):
            written = match.group(1)
            # topK is also Pinecone's, and every RAG file that calls Claude
            # has one. The camelCase spelling is reported for review.
            found.append((spellings[written], index, written != spellings[written]))
    return found


def scan(
    roots: Sequence[Path],
    catalog: Catalog,
    exclude: Optional[Set[Path]] = None,
    exclude_globs: Optional[Sequence[str]] = None,
    today: Optional[date] = None,
) -> ScanResult:
    # The date matters for `ignore until=`, and it has to be the same date the
    # report uses, so that `--today 2027-01-01` also shows the ignores that
    # will have expired by then.
    today = today or date.today()
    known_ids = {entry.id for entry in catalog.entries}
    id_pattern = _build_id_pattern(known_ids)
    path_pattern = _build_path_pattern(known_ids)
    header_patterns = _build_header_patterns(known_ids)
    embedded_pattern = _build_embedded_pattern(known_ids)

    param_names: Set[str] = set()
    certain_models: Set[str] = set()
    uncertain_models: Set[str] = set()
    for rule in catalog.parameters:
        param_names.update(rule.names)
        certain_models.update(rule.affects)
        uncertain_models.update(rule.affects_uncertain)
    spellings = _param_spellings(param_names)

    hits: List[Hit] = []
    param_hits: List[ParamHit] = []
    unknown: Dict[str, List[Hit]] = {}
    embedded: Dict[str, str] = {}
    skipped: List[Tuple[Path, str]] = []
    scanned = 0

    blocked: List[Path] = []
    for path in iter_files(roots, exclude, exclude_globs, blocked):
        text, problem = _read(path)
        if text is None:
            if problem:
                skipped.append((path, problem))
            continue
        if path.suffix == ".json" and _looks_like_a_catalog(text):
            continue
        scanned += 1
        suffix = path.suffix.lower()
        lines = _LINE_BREAK.split(text)

        file_ids: Set[str] = set()
        for index, line in enumerate(lines, start=1):
            if is_ignored(line, today):
                continue
            spans = []

            def record(identifier: str, start: int, end: int) -> None:
                file_ids.add(identifier)
                spans.append((start, end))
                hits.append(Hit(identifier, path, index, _excerpt(line, start)))

            if id_pattern is not None:
                for match in id_pattern.finditer(line):
                    identifier = match.group(1)
                    start, end = match.span(1)
                    if _BARE_WORD.fullmatch(identifier) and not _plausible_bare_id(line, start, end, suffix):
                        continue
                    record(identifier, start, end)
            # /v1/search is also Spotify's, and /v1/prompts can be your own
            # route. An endpoint counts only on a line that is about OpenAI.
            if path_pattern is not None and "openai" in line.lower():
                for match in path_pattern.finditer(line):
                    record(match.group(1), *match.span(1))
            for pattern, ident in header_patterns:
                for match in pattern.finditer(line):
                    record(ident, *match.span())
            embedded_spans: List[Tuple[int, int]] = []
            if embedded_pattern is not None:
                for match in embedded_pattern.finditer(line):
                    start, end = match.span(1)
                    if any(a <= start and end <= b for a, b in spans):
                        continue
                    begin = _token_start(line, start)
                    token = line[begin:end]
                    embedded_spans.append((begin, end))
                    embedded[token] = match.group(1)
                    unknown.setdefault(token, []).append(
                        Hit(token, path, index, _excerpt(line, start))
                    )

            # Identifiers that look like models but are not in the catalog. We
            # cannot tell the user anything about these, and saying so is the
            # point.
            for pattern in UNKNOWN_PATTERNS:
                for match in pattern.finditer(line):
                    candidate = match.group(0).rstrip(".-")
                    if candidate in known_ids:
                        continue
                    if any(a <= match.start() < b for a, b in embedded_spans):
                        continue
                    # There is deliberately no "is a piece of a known id" guard
                    # here: the regex is greedy and already captures the whole
                    # token, and the old guard hid gpt-4o just for being a  # depdesk: ignore
                    # prefix of gpt-4o-audio. Silencing a real finding beats  # depdesk: ignore
                    # no noise.
                    unknown.setdefault(candidate, []).append(
                        Hit(candidate, path, index, _excerpt(line, match.start()))
                    )

        if spellings:
            affected_certain = sorted(file_ids & certain_models)
            affected_uncertain = sorted(file_ids & uncertain_models)
            if affected_certain or affected_uncertain:
                occurrences = None
                if path.suffix == ".py":
                    occurrences = _find_params_python(text, lines, spellings)
                if occurrences is None:
                    occurrences = _find_params_text(lines, spellings)
                for name, line_no, soft in sorted(set(occurrences), key=lambda o: (o[1], o[0], o[2])):
                    line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
                    if is_ignored(line_text, today):
                        continue
                    param_hits.append(
                        ParamHit(
                            parameter=name,
                            path=path,
                            line=line_no,
                            excerpt=_excerpt(line_text),
                            models_in_file=affected_certain or affected_uncertain,
                            certain=bool(affected_certain) and not soft,
                        )
                    )

    skipped.extend((path, "directory could not be listed") for path in blocked)
    return ScanResult(
        hits=hits,
        param_hits=param_hits,
        unknown=unknown,
        files_scanned=scanned,
        files_skipped=len(skipped),
        embedded=embedded,
        skipped=skipped,
    )


def rule_for(catalog: Catalog, parameter: str) -> Optional[ParameterRule]:
    for rule in catalog.parameters:
        if parameter in rule.names:
            return rule
    return None
