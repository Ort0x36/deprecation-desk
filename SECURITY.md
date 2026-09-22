# Security

`depdesk` reads your source tree and prints what it finds. Two things are worth
knowing before you run it in an environment you care about:

- **It reads `.env` files**, because that is where model configuration lives. It
  prints only lines that matched a model identifier, but that line is printed as
  written. Scan a narrower path if that is a problem.
- **`depdesk upstream` makes network requests** to the provider documentation
  pages listed in the catalog, and nothing else. `depdesk check` and
  `depdesk list` never touch the network.

No telemetry, no analytics, no dependency that could add either.

## Reporting a vulnerability

Open a [security advisory](https://github.com/Ort0x36/deprecation-desk/security/advisories/new)
on this repository, or email the address on the maintainer's GitHub profile.
Please do not open a public issue for anything exploitable.
