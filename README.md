# depdesk

Know which of your model calls are on a clock.

Providers retire models on their schedule, not yours. The notice arrives by
email, to one person, months before anything breaks, and then it is forgotten
until a request starts failing in production. `depdesk` crosses the deprecation
notices you did not read with the models your code actually calls, and fails
your build while there is still time to do something about it.

Zero dependencies. Python 3.9 or newer.

```
pip install depdesk
depdesk check .
```

## What it looks like

```
$ depdesk check . --usage anthropic-export.csv --fail-in 60
depdesk: 412 files scanned in .
catalog verified 2026-09-20 (0 days ago)

ALREADY RETIRED

  claude-opus-4-1-20250805  [14.0% of measured calls]
      provider:    anthropic
      retirement:  2026-08-05 (46 days ago)
      announced:   2026-06-05
      replacement: claude-opus-4-8
      app/legacy.py:3: OLD = "claude-opus-4-1-20250805"

RETIRES SOON

  gpt-5.4-cyber  [3.0% of measured calls]
      provider:    openai
      retirement:  2026-10-01 (11 days left)
      announced:   2026-09-11
      replacement: gpt-5.6-cyber
      app/legacy.py:5: CYBER = "gpt-5.4-cyber"

DEPRECATED PARAMETERS

  temperature
      behaviour:   Returns a 400 error when set to a non-default value.
      replacement: Omit the parameter and steer behaviour through prompting.
      sdk:         The Python SDK (v1.0 and later) removes temperature, top_p
                   and top_k, so passing them raises a TypeError.
      app/legacy.py:12: temperature=0,
          file also names: claude-opus-5

Summary: 1 already retired, 1 retires soon.
$ echo $?
2
```

## Why the parameter section exists

Model retirement is the loud failure. The quiet one is a parameter that starts
returning 400 on newer models while your code keeps passing it, which is what
happened to `temperature`, `top_p` and `top_k` on Claude Opus 4.7 and later.
No changelog files that under "deprecation", and nothing in your test suite
catches it if your tests never hit the new model. `depdesk` parses your Python
with the standard library `ast` module, so it finds the keyword argument
itself, not a string that looks like one.

Where the provider's own wording is ambiguous about which models a rule covers,
`depdesk` says so and downgrades the finding to review instead of guessing.

## Use it in CI

This is the point of the exit codes.

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
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install depdesk
      - run: depdesk check . --fail-in 60
```

| code | meaning |
| --- | --- |
| 0 | nothing needs attention |
| 1 | something is deprecated, but outside the `--fail-in` window |
| 2 | something is already retired, or retires within `--fail-in` |
| 3 | the tool could not do its job |

## Real call volume

Scanning code tells you what is referenced. It does not tell you what is hot.
Point `--usage` at an export and every finding gains a share of traffic, so you
can tell a dead constant from 40% of production.

For Anthropic: Claude Console, **Usage**, **Export**. The CSV has an API key
column and a model column, and `depdesk` sniffs them.

```
depdesk check . --usage usage.csv
depdesk check . --usage usage.csv --usage-count-column input_tokens
```

CSV and JSON both work. Models that appear in usage but nowhere in your code
are still reported, because that is a call coming from somewhere you did not
look.

## Keeping the catalog honest

The only thing that makes this tool worth anything is whether the dates in it
are true. So they are not hidden: `depdesk/data/catalog.json` is plain JSON,
every entry was transcribed from the provider's own page, and the file records
when that happened.

```
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

`depdesk check` prints a warning when the catalog is more than 30 days old.

Catalog out of date? Open a pull request. That is the fastest path, and the
diff is readable by anyone.

## Commands

```
depdesk check [PATH ...]      scan a tree and report what is dying
depdesk upstream              ask whether the provider pages moved
depdesk list                  print the catalog
```

Useful flags on `check`:

| flag | what it does |
| --- | --- |
| `--fail-in DAYS` | how close a retirement has to be to fail the build (default 90) |
| `--sunset-in DAYS` | also report active models with an announced earliest retirement inside this window |
| `--usage FILE` | weight findings by real call volume |
| `--today YYYY-MM-DD` | ask what this repository looks like on a future date |
| `--json` | machine readable output |
| `--no-unknown` | stop reporting identifiers the catalog does not know |
| `--catalog FILE` | use your own catalog, for a provider we do not cover |

`--today` is more useful than it sounds: `depdesk check . --today 2027-01-01`
answers "what breaks over the holidays" before anyone leaves.

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

## Development

```
python3 -m pytest -q tests/
python3 -m depdesk check .
```

The test suite pins the behaviour that matters: that `gpt-4-turbo` does not
match inside `gpt-4-turbo-preview`, that an active model stays silent, that the
parameter rule needs an affected model in the same file before it fires, and
that the fingerprint survives cosmetic markup changes but not a changed date.

## Licence

MIT.
