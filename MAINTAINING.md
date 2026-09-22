# Maintaining depdesk

Notes for whoever keeps this alive. The tool is small; the obligation is the
catalog.

## The only recurring job

A stale catalog answers "nothing is deprecated" with confidence while a model
retires, which is the exact failure this tool exists to prevent. So:

- The **catalog drift** workflow runs every Monday, asks the provider pages
  whether they moved, and opens an issue when they did. If that issue appears,
  it is the highest priority work in this repository.
- Transcribe the change by hand, from the provider page, then:

  ```bash
  python3 -m depdesk upstream          # read what moved
  python3 -m depdesk upstream --save   # record the new baseline
  ```

  `--save` only records fingerprints. It does not write model entries: those
  are transcribed by a person, on purpose.
- `verified_on` is what the tool shows to users and what ages. Update it.

If the drift job itself starts failing every week (a page moved for good, a
fetch keeps timing out), fix the source entry rather than muting the job. The
first manual run of it opened an issue while both pages were unchanged: the
exit code counted the review queue as drift. Any change to `exit_code` in
`upstream.py` deserves the same suspicion, because the cost of being wrong
there is an alarm nobody reads.

## Cutting a release

1. Update `CHANGELOG.md` and the version in `pyproject.toml`.
2. Run the catalog check first: a release with a month old catalog is worse
   than no release.

   ```bash
   python3 -m pytest -q tests/
   python3 -m depdesk upstream
   python3 -m depdesk check .
   ```

3. Tag and publish:

   ```bash
   git tag -a v0.1.0 -m "0.1.0"
   git push origin v0.1.0
   python3 -m pip install --upgrade build twine
   python3 -m build
   python3 -m twine upload dist/*
   ```

4. Once the package is on PyPI, change the install line in `README.md` and in
   the CI snippet from the git URL to `pip install depdesk`, and add the PyPI
   badge. Until then the git URL is the honest instruction.

## Repository settings worth having

Topics, which is how people find a tool like this. In the web interface they
go under **About**, the gear icon on the repository home page:

```
llm, llmops, deprecation, model-lifecycle, openai, anthropic, claude,
ai-engineering, static-analysis, linter, cli, python, devtools, ci,
technical-debt
```

With the `gh` CLI installed:

```bash
gh repo edit --add-topic llm,llmops,deprecation,model-lifecycle,openai,\
anthropic,claude,ai-engineering,static-analysis,linter,cli,python,devtools,\
ci,technical-debt
```

With a token instead (classic token, `repo` scope; topics must be lowercase and
there is a limit of 20):

```bash
curl -X PUT https://api.github.com/repos/Ort0x36/deprecation-desk/topics \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -d '{"names":["llm","llmops","deprecation","model-lifecycle","openai","anthropic","claude","ai-engineering","static-analysis","linter","cli","python","devtools","ci","technical-debt"]}'
```

Also worth setting once: description ("Know which of your model calls are on a
clock"), website (blank is fine), and Issues enabled. Discussions are not worth
it at this size.
