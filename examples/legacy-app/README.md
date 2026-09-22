# examples/legacy-app

A deliberately broken sample: a retired model in a constant, one that retires
soon, a `temperature` argument on a model that no longer accepts it, and a
usage export where a retired model is still taking traffic from code that is
not here at all.

```bash
depdesk check examples/legacy-app --fail-in 60
depdesk check examples/legacy-app --usage examples/legacy-app/usage.csv
```

The first command exits 2. That is the point: this is what a failing build
looks like. CI runs exactly this, so the day the output stops matching, the
tool broke rather than the example.
