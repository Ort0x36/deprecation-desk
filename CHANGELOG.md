# Changelog

## 0.3.0

An outside review of `scan.py` found two ways to get a clean report from a
tree nobody had read. A full audit of every file that runs followed, and this
release fixes what it found. Several fixes make the tool read more than it did,
so a tree that passed 0.2.0 can have new findings here: that is the point, and
it is why this is 0.3.0 rather than a patch.

No more false all clear:

- A root under a directory called `build`, `dist`, `vendor` or `target`
  (`depdesk check /build/app`) read zero files and printed "Nothing deprecated
  found". The skip list now applies only below the root. A directory in which
  no file can be read exits 3 instead of 0.
- Files that were not UTF-8, UTF-16 files and anything over 2 MB were dropped
  without a word. They are read now (model ids are ASCII), the size limit is 20
  MB, and whatever still cannot be read is listed under `COULD NOT READ`, in
  the JSON and in the job summary, and counts as a warning. Binaries are
  recognised before the size check, so model weights never land there, and a
  directory that cannot be listed is reported instead of crashing the walk.
- `env` directories are read (`deploy/env/production.env`); virtualenvs are
  recognised by `pyvenv.cfg` instead of by name. Extensionless files such as
  `.envrc`, `Jenkinsfile` and `bin/` scripts are read in the tree walk, as
  pre-commit already did.
- `--exclude` globs no longer match the directories above the working
  directory, which made `--exclude "*/docs/*"` drop every file of a
  repository called docs. They match the path as written, relative to the
  working directory and to the root, and any tail of it, the way .gitignore
  does, so a pattern means the same in a tree walk and in pre-commit.
- An id that ends a sentence, `calls claude-3-opus-20240229.`, is found. A dot
  before a quote or a star is a prefix check (`startswith("gpt-4.")`) and still
  is not.
- A catalog id glued to a prefix, `anthropic.claude-2.0` or `prod-gpt-4`, is
  reported as not in the catalog, with the model it contains. It is not given
  the provider's date, because cloud platforms run their own schedules.
- Deprecated parameters are found in JSON, TypeScript and curl payloads
  (`"temperature": 0`) and in Python request dicts passed as `**params`. The
  camelCase the JavaScript SDKs take (`topP`, which is also Pinecone's) and
  item assignment (`kwargs["top_p"]`) are reported for review, never as a
  failure. A tool schema's `"temperature": {"type": "number"}` is not a
  request. A Python file
  with a byte order mark, or one this interpreter cannot parse, falls back to
  the text rules instead of losing parameter detection.
- Line numbers no longer drift after a form feed, which also made the ignore
  pragma read the wrong line.
- Mistyped pragmas (`ignore until 2026-12-01`, `until:`, `ignore-until=`,
  `ignored`) silence nothing, as the README promised, instead of becoming
  permanent ignores.
- The catalog gained the models, endpoints and aliases the provider pages list
  and it did not: `o1-mini`, `o1-preview`, `gpt-4-turbo-preview`, the 2024-10-01
  realtime and audio previews, the InstructGPT, edit, Codex and first
  generation embedding models, `/v1/edits` and the 2022 endpoints, the Videos
  API, `ft:babbage-002` and `ft:davinci-002` with their own date, and the
  Anthropic alias `claude-haiku-4-5`. The bare base models ada, babbage, curie
  and davinci are left out on purpose: in quotes they are more often a name
  than a model.
- Endpoints match inside a full URL on a line that is about OpenAI (`/v1/search`
  is also Spotify's), and the `OpenAI-Beta` header the way code writes it, as a
  dict entry, a tuple or an item assignment.
- The GitHub Action runs the depdesk that ships with the ref instead of
  installing one. It used to run pip into the system Python, which Ubuntu 24.04
  and Debian refuse, and to reuse any depdesk already on PATH, so a runner with
  an old install used the old catalog. `version:` installs a release with pip
  `--target`. `args` is split like a shell would split it: a second line, or
  quotes, used to be dropped or passed through literally.

No more wrong failures:

- The catalog id `o1` failed the build on `def overlap(o1, o2)`. An id with no
  hyphen now counts when it is quoted, the value of a key or flag that names a
  model (`ENV MODEL=o1`, `model: [o1, gpt-4o]`, `--model o1`), or, outside
  code, a config value or a YAML list item.
- Invocation errors exit 3: a mistyped flag exited 2 (retired), an invalid
  `--today` exited 1 (warnings). A broken `--catalog` exits 3 with a message
  instead of a traceback. Negative `--fail-in`, `--sunset-in` and `--locations`
  are refused.
- `--sunset-in 0` is off, as documented; it reported every passed earliest
  retirement date.
- Output no longer crashes on a stdout that cannot encode a character, which
  is a Windows pipe.

Reporting:

- The summary counts deprecated parameters and unread files; a run with only a
  parameter said "nothing to report" and exited 2.
- A replacement that is itself retired or deprecated says so and names the
  living one (`chatgpt-4o-latest` points at `gpt-5.1-chat-latest`, which is
  retired too).
- `--locations 0` no longer removes the GitHub annotations of a failing build.
- Overlapping roots scan each file once.
- The action exposes `outputs.exit-code`, and CI asserts the exact code instead
  of "the step failed".
- The README sample output is checked against the tool by the test suite. It
  had drifted: wrong line numbers and a summary one finding short.

`--usage`:

- The count column is the first likely one that holds numbers. The hint "n"
  matched any column with the letter n, so `api_key_name` was summed and a
  retired model with 90% of the traffic showed 0.0%. `usage_date_utc` matched
  "usage", and `model_version` was picked as both model and count.
- Semicolon separated CSV (with the European `1.420` and `0,50` number
  forms it comes with), a byte order mark, and JSON rows under `"data"` with
  any count key work. NaN, infinite and negative counts are ignored. A named column that does not exist, or a count column with no
  positive number, is an error instead of a silent 0.0%.

`upstream`:

- Date changes on the OpenAI page were invisible: abbreviated months ("Aug 10,
  2026") and dates written with non-breaking hyphens were not extracted, and a
  row moving to a date already on the page changed nothing. The fingerprint is
  now the ordered sequence of ids and dates, taken only from the deprecation
  list, so a blog post title in the navigation cannot raise the alarm.
- An empty or blocked page is "could not check" (exit 3) and cannot be saved as
  the baseline. A crash while fetching exits 3, not 1, which the drift workflow
  read as "the page moved". `--save` exits 3 when a source failed.

Release:

- The release workflow builds and tests in a job with no publishing rights and
  publishes from a separate job that only runs the PyPI action.
- The pre-commit hook runs its batches one after the other instead of in
  parallel, so a CI job summary is one table for an ordinary commit. A very
  long file list is still split by pre-commit, one table per batch.
- README links are absolute, because PyPI renders the same file.

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
