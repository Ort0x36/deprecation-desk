# Changelog

## 0.1.0

First public release.

- `depdesk check`: scans a tree for model identifiers, deprecated parameters,
  endpoints and products, and exits with a code CI can act on.
- `depdesk upstream`: compares the provider deprecation pages against the
  catalog and reports what appeared or disappeared, without pretending to
  parse them into a catalog by itself.
- `depdesk list`: prints the catalog.
- `--exclude` globs and a `depdesk: ignore` line marker, so prose that names
  a dead model on purpose does not have to be a finding forever.
- `--usage` weights every finding by real call volume, from an Anthropic or
  OpenAI usage export.
- Catalog covering Anthropic and OpenAI, transcribed from the provider pages
  on 2026-09-20.
