# Changelog

## 0.1.3

- Install instructions that survive PEP 668. `pip install depdesk` into the
  system Python fails on Ubuntu 24.04 and Debian, which is where a good share
  of readers are, and the README offered exactly two paths: `uvx`, for people
  who already have uv, and that failing `pip`. Both dead ends for the same
  person. The README now installs uv in one line and documents pipx and a
  virtual environment.

## 0.1.2

- `depdesk --version` reports the version it actually is. 0.1.1 shipped
  saying 0.1.0, because the number lives in `pyproject.toml` and in
  `__init__.py` and only one was bumped. A test now fails when they drift.

## 0.1.1

- A copy of the catalog sitting in the scanned tree is skipped, not reported.
  The catalog in use was already excluded by path, so `depdesk check` passed
  from a source checkout and failed from an installed wheel, on the same
  command and the same files. A catalog is a list of dead models by
  definition, and reporting it says nothing.

- A pre-commit hook, so adopting this is three lines in a config people
  already have.
- A GitHub Action, `Ort0x36/deprecation-desk@v1`, for the same reason. CI
  runs it against the broken sample and against the package itself, so the
  action is tested in both directions.
- `uvx depdesk check .` documented first: it is the shortest path from
  reading about this to knowing whether your own repository is affected.

## 0.1.0

First public release.

- `depdesk check`: scans a tree for model identifiers, deprecated parameters,
  endpoints and products, and exits with a code CI can act on.
- `depdesk upstream`: compares the provider deprecation pages against the
  catalog and reports what appeared or disappeared, without pretending to
  parse them into a catalog by itself. It fails only when a page actually
  moved; `--strict` also fails on identifiers the catalog does not mention.
- `depdesk list`: prints the catalog.
- `--exclude` globs and a `depdesk: ignore` line marker, so prose that names
  a dead model on purpose does not have to be a finding forever.
- `--usage` weights every finding by real call volume, from an Anthropic or
  OpenAI usage export.
- Catalog covering Anthropic and OpenAI, transcribed from the provider pages
  on 2026-09-20.
