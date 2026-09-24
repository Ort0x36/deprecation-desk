# Changelog

## 0.2.0

- `--fail-in` decides what fails the build, which is what the README always
  said it did. Up to 0.1.4 a deprecation outside the window exited 1, and CI,
  the action and pre-commit all fail on any non zero code, so a model retiring
  in five months broke the build exactly like one retiring tomorrow. Now
  anything already retired or retiring inside the window exits 2, as before,
  and anything further out is printed as a warning and exits 0. This changes
  the exit code for the softer findings, hence the minor version. `--strict`
  brings back a failure on warnings, with exit 1, for whoever wants every
  deprecation to block.
- Inside GitHub Actions, findings become annotations on the line that uses
  them and a table in the job summary. With warnings no longer failing the
  build, they needed somewhere to be seen other than the log of a green job.
  On by default when `GITHUB_ACTIONS` is set; `--no-github` turns it off. The
  JSON output never carries annotations, so piping it into jq keeps working.
- `depdesk: ignore until=2026-12-01` silences a line only until that date, so a
  postponed fix cannot quietly turn into a permanent exception. A date that
  does not parse silences nothing, and `--today` applies to it.
- The action takes `strict: "true"`.
- JSON findings say whether each one fails the build (`fails_build`), and the
  report carries `fail_in` and `strict`.
- Catalog verified again on 2026-09-24, because both provider pages moved
  since the last release. Anthropic lists `claude-opus-5-5`, which is also
  covered by the `temperature`/`top_p`/`top_k` rule. The OpenAI page names
  the aliases next to each snapshot and the substitute for every legacy audio
  and realtime model, so the catalog gained `gpt-4`, `gpt-3.5-turbo`, `o1`,
  `o1-pro`, `o3-mini`, `o4-mini` and the other aliases shutting down on
  2026-10-23, plus the replacements it was missing. `o3-mini-2025-01-31` now
  points at `gpt-5.6-sol`, as the page says. Code that calls `gpt-4` or
  `o4-mini` by alias passed the check before this release.

## 0.1.4

- Words people actually search for, in the places that get indexed. The
  package summary and the line under the title said "know which of your model
  calls are on a clock", which reads well and carries none of the terms
  somebody types: deprecated, retired, model retirement. An abandoned tool
  with the same idea outranked this one on GitHub search for exactly that
  reason.

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
