# Contributing

The valuable contribution here is not code. It is keeping the catalog true.

## Updating the catalog

`depdesk/data/catalog.json` is plain JSON on purpose, so that a correction is a
readable diff instead of a scraper run.

1. Open the provider's own deprecation page. Not a blog post about it, not a
   changelog summary: the page the provider maintains.
2. Copy the dates as the provider writes them. If the page says a model is
   deprecated but names no retirement date, leave the date out rather than
   estimating one.
3. Update `verified_on` for the source you touched, at the top of the file.
4. Run the tests, then run the tool on itself:

   ```bash
   python3 -m pytest -q tests/
   python3 -m depdesk check .
   ```

5. Open the pull request with the link to the page in the description.

If the provider page moved but you are unsure what the change means, open an
issue instead. A wrong entry is worse than a missing one: the whole promise of
this tool is that a clean run means something.

## Adding a provider

One hard requirement: the provider publishes a deprecation or model lifecycle
page. Without it there is no honest source, and a catalog assembled from rumour
is exactly what this tool exists to replace.

Add to `catalog.json`:

- an entry under `sources` with the URL and `verified_on`, so `depdesk upstream`
  can watch the page;
- one entry per model under `models`, with `id`, `provider`, `status`
  (`active`, `deprecated` or `retired`), the dates the page gives, and the
  replacement it names.

Run `depdesk upstream` afterwards: it will tell you which identifiers on the
page the catalog does not mention yet.

## Changing the scanner

Two failure modes, and they are not equal:

- a **false positive** reports something that is fine. Annoying.
- a **false negative** misses a model id you really call. That is the failure
  this tool cannot afford, and it is why the scanner errs towards reporting
  unknown identifiers rather than staying quiet.

Any change to matching needs a test in `tests/test_depdesk.py`. The existing
ones show the shape: `gpt-4-turbo` must not match inside `gpt-4-turbo-preview`,
an active model must stay silent, the parameter rule must not fire without an
affected model in the same file.

## Style

- No dependencies. The standard library has been enough so far, and "runs
  anywhere with a Python" is a feature of this tool.
- Comments explain *why*, usually by naming the bug that caused the line.
- Output is read by a tired person at build time. Say what is wrong, where it
  is, and what to do about it.
