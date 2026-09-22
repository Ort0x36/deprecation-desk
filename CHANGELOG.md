# Changelog

## Unreleased

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
