# depdesk

**Know which of your model calls are on a clock.**

[![ci](https://github.com/Ort0x36/deprecation-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/Ort0x36/deprecation-desk/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/depdesk)](https://pypi.org/project/depdesk/)
[![python](https://img.shields.io/badge/python-3.9%20to%203.13-blue)](https://www.python.org/downloads/)
[![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-blue)](LICENSE)

Providers retire models on their schedule, not yours. The notice arrives by
email, to one person, months before anything breaks, and then it is forgotten
until a request starts failing in production. `depdesk` crosses the deprecation
notices you did not read with the models your code actually calls, and fails
your build while there is still time to do something about it.

Zero dependencies. Python 3.9 or newer. Anthropic and OpenAI today, and any
provider you write a catalog entry for.

## Try it in ten seconds

With [uv](https://docs.astral.sh/uv/), there is nothing to install:

```bash
uvx depdesk check .
```

No uv? One line, no sudo:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or install depdesk itself, with pipx or into a virtual environment:

```bash
pipx install depdesk

# or
python3 -m venv .venv
.venv/bin/pip install depdesk
.venv/bin/depdesk check .
```

A plain `pip install depdesk` into the system Python fails on Ubuntu 24.04,
Debian and everything else that ships [PEP 668](https://peps.python.org/pep-0668/),
which is most distributions now. That is the distribution protecting its own
Python, not this package being difficult, and every command above works around
it the way the distribution intends.

That is the whole setup. Point it at a repository and it reports every model
identifier it finds, crossed against the provider's published deprecation
dates. There is a deliberately broken sample in
[`examples/legacy-app`](examples/legacy-app) if you want to see a failing run
before pointing it at your own code.

```
$ depdesk check . --fail-in 60
depdesk: 2 files scanned in .
catalog verified 2026-09-20 (2 days ago)

ALREADY RETIRED

  claude-opus-4-1-20250805
      provider:    anthropic
      retirement:  2026-08-05 (48 days ago)
      announced:   2026-06-05
      replacement: claude-opus-4-8
      app/legacy.py:1: OLD = "claude-opus-4-1-20250805"

RETIRES SOON

  gpt-5.4-cyber
      provider:    openai
      retirement:  2026-10-01 (9 days left)
      announced:   2026-09-11
      replacement: gpt-5.6-cyber
      app/legacy.py:2: CYBER = "gpt-5.4-cyber"

DEPRECATED PARAMETERS

  temperature
      behaviour:   Returns a 400 error when set to a non-default value.
      replacement: Omit the parameter and steer behaviour through prompting.
      sdk:         The Python SDK (v1.0 and later) removes temperature, top_p
                   and top_k, so passing them raises a TypeError.
      app/legacy.py:9: temperature=0,
          file also names: claude-opus-5

Summary: 1 already retired, 1 retires soon.
$ echo $?
2
```

Every line of that output is real: it is the tool running against a two file
sample, not a mockup.

## What it finds

**Models on a clock.** Identifiers written anywhere in the tree, including
`.env` files and config, matched against the provider's own retirement dates.
Each finding carries the announcement date, the retirement date, the
replacement the provider names, and the exact lines where you use it.

**Deprecated parameters.** Model retirement is the loud failure. The quiet one
is a parameter that starts returning 400 on newer models while your code keeps
passing it, which is what happened to `temperature`, `top_p` and `top_k` on
Claude Opus 4.7 and later. No changelog files that under "deprecation", and
nothing in your test suite catches it if your tests never hit the new model.
`depdesk` parses your Python with the standard library `ast` module, so it
finds the keyword argument itself, not a string that looks like one.

**Endpoints and products** that the provider has put on notice, matched the
same way.

Where the provider's own wording is ambiguous about which models a rule covers,
`depdesk` says so and downgrades the finding to review instead of guessing.

## Put it in a commit hook

For [pre-commit](https://pre-commit.com), three lines in the config you already
have. It checks the staged files, which is fast enough to sit in a commit hook;
scanning the whole tree is CI's job.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Ort0x36/deprecation-desk
    rev: v0.1.3
    hooks:
      - id: depdesk
```

## Put it in CI

This is the point of the exit codes: the build starts failing the day a
provider gives you notice, not the day the model dies.

```yaml
# .github/workflows/depdesk.yml
name: model deprecations
on:
  push:
  schedule:
    - cron: "0 7 * * 1"   # Monday morning, so notice arrives before the deadline

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with: { python-version: "3.12" }
      - run: pip install depdesk
      - run: depdesk check . --fail-in 60
```

There is also an action, if you prefer it to the two lines above:

```yaml
      - uses: Ort0x36/deprecation-desk@v1
        with:
          path: .
          fail-in: "60"
          # usage: usage.csv
```

| code | meaning |
| --- | --- |
| 0 | nothing needs attention |
| 1 | something is deprecated, but outside the `--fail-in` window |
| 2 | something is already retired, or retires within `--fail-in` |
| 3 | the tool could not do its job |

## Weight it by real traffic

Scanning code tells you what is referenced. It does not tell you what is hot.
Point `--usage` at an export and every finding gains a share of traffic, so you
can tell a dead constant from 40% of production.

```bash
depdesk check . --usage usage.csv
depdesk check . --usage usage.csv --usage-count-column input_tokens
```

```
ALREADY RETIRED

  claude-3-5-sonnet-20241022  [3.3% of measured calls]
      provider:    anthropic
      retirement:  2025-10-28 (329 days ago)
      announced:   2025-08-13
      replacement: claude-sonnet-4-6
      seen in usage data but not found anywhere in the scanned tree

  claude-opus-4-1-20250805  [15.3% of measured calls]
      provider:    anthropic
      retirement:  2026-08-05 (48 days ago)
      announced:   2026-06-05
      replacement: claude-opus-4-8
      app/legacy.py:1: OLD = "claude-opus-4-1-20250805"
```

That first finding is the one worth the flag: a retired model still taking
traffic from code that is not in this repository at all.

For Anthropic: Claude Console, **Usage**, **Export**. The CSV has an API key
column and a model column, and `depdesk` sniffs them. CSV and JSON both work.

## Ask what breaks later

```bash
depdesk check . --today 2027-01-01
```

More useful than it sounds: it answers "what breaks over the holidays" before
anyone leaves.

## When a finding is not a finding

Documentation, changelogs and migration notes legitimately name models that are
dead. A tool you cannot silence on a line you have already judged is a tool
people stop running, so there are two escapes, both deliberate:

```python
OLD = "claude-opus-4-1-20250805"  # depdesk: ignore
```

```bash
depdesk check . --exclude "CHANGELOG.md" --exclude "*/migrations/*"
```

The marker silences one line, in any file type, in whatever comment syntax that
file uses, because it is matched as text. `--exclude` takes a glob, matched
against the path and the file name, and repeats. Neither has a project level
config file on purpose: an ignore list nobody reads is how a tool like this
starts lying.

## Commands and flags

```
depdesk check [PATH ...]      scan a tree and report what is dying
depdesk upstream              ask whether the provider pages moved
depdesk list                  print the catalog
```

| flag on `check` | what it does |
| --- | --- |
| `--fail-in DAYS` | how close a retirement has to be to fail the build (default 90) |
| `--sunset-in DAYS` | also report active models with an announced earliest retirement inside this window |
| `--usage FILE` | weight findings by real call volume |
| `--usage-model-column`, `--usage-count-column` | override the sniffed columns |
| `--today YYYY-MM-DD` | ask what this repository looks like on a future date |
| `--locations N` | how many source lines to print per finding (default 3) |
| `--json` | machine readable output |
| `--exclude GLOB` | skip paths matching this glob, repeatable |
| `--no-unknown` | stop reporting identifiers the catalog does not know |
| `--catalog FILE` | use your own catalog, for a provider we do not cover |

## The catalog, and why you can trust it

The only thing that makes this tool worth anything is whether the dates in it
are true. So they are not hidden: [`depdesk/data/catalog.json`](depdesk/data/catalog.json)
is plain JSON, every entry was transcribed from the provider's own page, and
the file records when that happened. `depdesk check` prints a warning when the
catalog is more than 30 days old.

```bash
depdesk upstream          # did the provider pages change since then?
depdesk upstream --save   # record the current state as the new baseline
```

`upstream` does not parse the page into a catalog. An earlier version hashed
the whole page and produced a different digest on three consecutive fetches of
the same unchanged content, because provider docs carry per-request markup. So
it extracts the model identifiers and the dates, compares those against the
catalog, and prints what appeared and what disappeared. A human decides what it
means. A scraper that half works would produce a catalog that looks maintained
and is not, which is the exact failure this tool exists to prevent.

`upstream` exits 1 when a page moved, 3 when it could not be fetched, and 0
otherwise. It prints, but does not fail on, the identifiers a page names that
the catalog does not mention: the OpenAI page alone names 35 of those and
always will, because a deprecation page also lists models that are not being
deprecated. That is a standing difference, not an event, and a weekly alarm
that fires on it stops being read. Pass `--strict` if you do want those to
fail.

This repository runs `upstream` every Monday and
[opens an issue](.github/workflows/catalog-drift.yml) when a provider page
moves, so drift lands in front of a person instead of in a log nobody reads.

## What it does not do

Being plain about the limits, because the whole point is to be trustworthy:

- **It does not know your provider's private schedule.** It knows what is on the
  public deprecation pages, transcribed by hand.
- **It only covers Anthropic and OpenAI today.** Other providers are a catalog
  entry away, and pull requests are welcome. `--catalog` takes your own file.
- **It finds identifiers written as text.** A model id assembled at runtime from
  string pieces will not be found by the scanner. The `--usage` path catches
  that case, which is part of why it exists.
- **It reads `.env` files**, because that is where model configuration lives. It
  prints only lines that matched a model identifier, but if that bothers you,
  scan a narrower path.
- **It will not edit your code.** Migrating a prompt is a judgement call and this
  tool has no opinion about your prompts.

## Contributing

Catalog out of date? A pull request editing `depdesk/data/catalog.json` is the
fastest path, and the diff is readable by anyone. Want a provider we do not
cover? Open an issue with a link to their public deprecation page. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
python3 -m pytest -q tests/
python3 -m depdesk check .
```

The test suite pins the behaviour that matters: that `gpt-4-turbo` does not
match inside `gpt-4-turbo-preview`, that an active model stays silent, that the
parameter rule needs an affected model in the same file before it fires, and
that the fingerprint survives cosmetic markup changes but not a changed date.

## Licence

MIT. Built by [Wendel Ortiz](https://github.com/Ort0x36) while running an LLM
product in production.
