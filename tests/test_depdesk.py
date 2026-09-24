"""Tests. Run with: python3 -m pytest -q  (or python3 tests/test_depdesk.py)"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from depdesk import catalog as catalog_mod  # noqa: E402
from depdesk import github  # noqa: E402
from depdesk.__main__ import main  # noqa: E402
from depdesk.report import build, render_json, render_text  # noqa: E402
from depdesk.scan import is_ignored, scan  # noqa: E402
from depdesk.upstream import UpstreamResult, exit_code, fingerprint  # noqa: E402
from depdesk.usage import load_usage, share  # noqa: E402

TODAY = date(2026, 9, 20)


def _catalog():
    return catalog_mod.load()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _report(tmp_path: Path, fail_in: int = 90, today: date = TODAY, **kwargs):
    cat = _catalog()
    result = scan([tmp_path], cat, today=today)
    return build(result, cat, today=today, fail_in=fail_in, sunset_in=0, roots=[tmp_path], **kwargs)


def test_catalog_loads_and_is_consistent():
    cat = _catalog()
    assert cat.entries, "catalog is empty"
    for entry in cat.entries:
        if entry.retires_on and entry.deprecated_on:
            assert entry.retires_on >= entry.deprecated_on, f"{entry.id} retires before announcement"
        if entry.status == "active":
            assert entry.retires_on is None, f"{entry.id} is active but has a retirement date"


def test_retired_model_is_found(tmp_path):
    _write(tmp_path, "a.py", 'MODEL = "claude-opus-4-1-20250805"\n')
    report = _report(tmp_path)
    assert [f.identifier for f in report.findings] == ["claude-opus-4-1-20250805"]
    finding = report.findings[0]
    assert finding.severity == "retired"
    assert finding.days_left == -46
    assert finding.replacement == "claude-opus-4-8"
    assert report.exit_code() == 2


def test_deadline_window_moves_severity(tmp_path):
    _write(tmp_path, "a.py", 'M = "gpt-image-1.5"\n')  # retires 2026-12-01, 72 days out
    assert _report(tmp_path, fail_in=30).findings[0].severity == "deprecated"
    assert _report(tmp_path, fail_in=90).findings[0].severity == "due"


def test_active_model_is_silent(tmp_path):
    _write(tmp_path, "a.py", 'M = "claude-sonnet-4-6"\n')
    report = _report(tmp_path)
    assert report.findings == []
    assert report.exit_code() == 0


def test_prefix_does_not_false_match(tmp_path):
    # gpt-4-turbo is in the catalog; gpt-4-turbo-experimental is a different
    # string and must not be reported as if it were the known one.
    _write(tmp_path, "a.py", 'M = "gpt-4-turbo-experimental"\n')
    report = _report(tmp_path)
    ids = [f.identifier for f in report.findings]
    assert "gpt-4-turbo" not in ids
    assert "gpt-4-turbo-experimental" in ids
    assert report.findings[0].severity == "unknown"


def test_unknown_model_is_reported(tmp_path):
    _write(tmp_path, "a.py", 'M = "claude-quasar-9"\n')
    report = _report(tmp_path)
    assert report.findings[0].severity == "unknown"
    assert report.exit_code() == 0  # unknown alone is not a failure


def test_parameter_rule_needs_an_affected_model(tmp_path):
    affected = 'M = "claude-opus-5"\nclient.create(model=M, temperature=0)\n'
    _write(tmp_path, "hot.py", affected)
    report = _report(tmp_path)
    assert [h.parameter for h in report.param_hits] == ["temperature"]
    assert report.param_hits[0].certain is True
    assert report.exit_code() == 2


def test_parameter_rule_quiet_on_unaffected_model(tmp_path):
    _write(tmp_path, "cold.py", 'M = "claude-sonnet-4-6"\nclient.create(model=M, temperature=0)\n')
    report = _report(tmp_path)
    assert report.param_hits == []


def test_parameter_rule_uncertain_scope_is_softer(tmp_path):
    _write(tmp_path, "grey.py", 'M = "claude-sonnet-5"\nclient.create(model=M, top_p=0.9)\n')
    report = _report(tmp_path)
    assert report.param_hits and report.param_hits[0].certain is False
    assert report.exit_code() == 0  # review, not failure
    assert _report(tmp_path, strict=True).exit_code() == 1


def test_skips_vendor_directories(tmp_path):
    _write(tmp_path, "node_modules/x.js", 'const m = "claude-opus-4-1-20250805";\n')
    _write(tmp_path, "keep.py", "x = 1\n")
    report = _report(tmp_path)
    assert report.findings == []


def test_usage_csv_is_summed_and_shared(tmp_path):
    path = _write(
        tmp_path,
        "u.csv",
        "api_key,model,requests\nk1,claude-opus-5,750\nk2,claude-opus-5,50\nk3,gpt-4-turbo,200\n",
    )
    totals, column = load_usage(path)
    assert totals == {"claude-opus-5": 800.0, "gpt-4-turbo": 200.0}
    assert column == "requests"
    assert share(totals)["gpt-4-turbo"] == 0.2


def test_usage_json_object(tmp_path):
    path = _write(tmp_path, "u.json", json.dumps({"gpt-4-turbo": 3, "claude-opus-5": 1}))
    totals, _ = load_usage(path)
    assert totals["gpt-4-turbo"] == 3.0


def test_usage_only_model_still_gets_a_verdict(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    cat = _catalog()
    result = scan([tmp_path], cat)
    report = build(
        result, cat, today=TODAY, fail_in=90, sunset_in=0,
        usage={"gpt-4-turbo": 10.0}, usage_shares={"gpt-4-turbo": 1.0}, roots=[tmp_path],
    )
    finding = report.findings[0]
    assert finding.identifier == "gpt-4-turbo"
    assert finding.locations == []
    assert "usage data" in render_text(report)


def test_json_output_is_valid_and_carries_exit_code(tmp_path):
    _write(tmp_path, "a.py", 'M = "claude-opus-4-1-20250805"\n')
    payload = json.loads(render_json(_report(tmp_path)))
    assert payload["exit_code"] == 2
    assert payload["findings"][0]["retires_on"] == "2026-08-05"
    assert payload["catalog"]["verified_on"]


def test_today_override_changes_the_verdict(tmp_path):
    src = _write(tmp_path, "a.py", 'M = "gpt-5.4-cyber"\n')  # retires 2026-10-01
    cat = _catalog()
    result = scan([tmp_path], cat)
    before = build(result, cat, today=date(2026, 9, 1), fail_in=10, sunset_in=0)
    after = build(result, cat, today=date(2026, 10, 2), fail_in=10, sunset_in=0)
    assert before.findings[0].severity == "deprecated"
    assert after.findings[0].severity == "retired"
    assert src.exists()


def test_fingerprint_ignores_markup_noise():
    a = b"<html><script>var build='1'</script><p>Opus 4.1 retires 2026-08-05</p></html>"
    b = b"<html>  <script>var build='2'</script>\n<p>Opus   4.1 retires 2026-08-05</p></html>"
    c = b"<html><p>Opus 4.1 retires 2026-09-09</p></html>"
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint(c)


def test_cli_exit_codes(tmp_path, capsys):
    _write(tmp_path, "a.py", 'M = "claude-opus-4-1-20250805"\n')
    assert main(["check", str(tmp_path)]) == 2
    _write(tmp_path / "clean", "b.py", 'M = "claude-sonnet-4-6"\n')
    assert main(["check", str(tmp_path / "clean")]) == 0
    assert main(["check", str(tmp_path / "does-not-exist")]) == 3
    capsys.readouterr()


def test_cli_defaults_to_check(tmp_path, capsys):
    _write(tmp_path, "a.py", 'M = "claude-sonnet-4-6"\n')
    assert main([str(tmp_path)]) == 0
    capsys.readouterr()


if __name__ == "__main__":  # pragma: no cover
    import subprocess

    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))


# Regressoes encontradas rodando a ferramenta contra o proprio repositorio.

def test_catalog_file_is_never_scanned(tmp_path, capsys):
    """O catalogo e uma lista de modelos mortos: varre-lo reporta tudo."""
    # A pasta so tem o catalogo, entao nada e lido: desde a 0.2.1 isso e 3,
    # "nada foi conferido", e nao um 0 que ninguem mereceu.
    assert main(["check", str(Path(catalog_mod.DEFAULT_CATALOG).parent)]) == 3
    assert "claude" not in capsys.readouterr().out


def test_trailing_punctuation_is_not_part_of_the_identifier(tmp_path):
    _write(tmp_path, "a.py", "# see gpt-4o.\n")
    report = _report(tmp_path)
    assert [f.identifier for f in report.findings] == ["gpt-4o"]


def test_ignore_pragma_silences_a_line(tmp_path):
    # Documentation and migration notes legitimately name dead models. A line
    # the reader has already judged must be silenceable, or the tool gets
    # switched off entirely.
    _write(
        tmp_path,
        "notes.md",
        'We used to call "claude-opus-4-1-20250805" here.  <!-- depdesk: ignore -->\n',
    )
    report = _report(tmp_path)
    assert report.findings == []
    assert report.exit_code() == 0


def test_ignore_pragma_silences_a_deprecated_parameter(tmp_path):
    _write(
        tmp_path,
        "a.py",
        'M = "claude-opus-5"\n'
        "client.messages.create(model=M, temperature=0)  # depdesk: ignore\n",
    )
    report = _report(tmp_path)
    assert report.param_hits == []


def test_ignore_pragma_does_not_silence_the_next_line(tmp_path):
    _write(
        tmp_path,
        "a.py",
        '# depdesk: ignore\nM = "claude-opus-4-1-20250805"\n',
    )
    assert [f.identifier for f in _report(tmp_path).findings] == ["claude-opus-4-1-20250805"]


def test_exclude_glob_skips_matching_files(tmp_path):
    _write(tmp_path, "app/live.py", 'M = "claude-opus-4-1-20250805"\n')
    _write(tmp_path, "docs/history.md", 'We shipped on "claude-3-opus-20240229".\n')

    cat = _catalog()
    everything = scan([tmp_path], cat)
    assert len({hit.identifier for hit in everything.hits}) == 2

    without_docs = scan([tmp_path], cat, exclude_globs=["*/docs/*"])
    assert {hit.identifier for hit in without_docs.hits} == {"claude-opus-4-1-20250805"}

    by_name = scan([tmp_path], cat, exclude_globs=["history.md"])
    assert {hit.identifier for hit in by_name.hits} == {"claude-opus-4-1-20250805"}


def _result(**kwargs) -> UpstreamResult:
    base = dict(provider="openai", url="https://example.invalid", ok=True, changed=False)
    base.update(kwargs)
    return UpstreamResult(**base)


def test_review_queue_alone_is_not_drift():
    # The OpenAI page names 35 identifiers the catalog does not, and always
    # will, because a deprecation page also lists models that are not being
    # deprecated. Exiting 1 on that opened an issue every Monday saying the
    # pages had changed when they had not.
    results = [_result(new_on_page=["gpt-4-turbo-preview"], missing_from_page=["omni-moderation"])]
    assert exit_code(results) == 0
    assert exit_code(results, strict=True) == 1


def test_a_moved_page_is_drift():
    assert exit_code([_result(changed=True)]) == 1
    assert exit_code([_result(changed=False)]) == 0


def test_a_failed_fetch_beats_everything():
    assert exit_code([_result(ok=False, error="timeout"), _result(changed=True)]) == 3


def test_a_copy_of_the_catalog_is_not_scanned(tmp_path, capsys):
    # Running from an installed wheel put the catalog in use outside the tree,
    # so this repository's own copy stopped being excluded by path and turned
    # into a page of findings about itself. Same command, different result
    # depending on how you installed it.
    copy = json.loads(catalog_mod.load().path.read_text(encoding="utf-8"))
    _write(tmp_path, "vendor/catalog.json", json.dumps(copy))
    _write(tmp_path, "app.py", 'M = "claude-sonnet-5"\n')
    report = _report(tmp_path)
    assert report.findings == []
    assert report.exit_code() == 0


def test_a_json_file_that_is_not_a_catalog_still_counts(tmp_path):
    _write(tmp_path, "config.json", '{"model": "claude-opus-4-1-20250805"}\n')
    assert [f.identifier for f in _report(tmp_path).findings] == ["claude-opus-4-1-20250805"]


def test_the_two_version_literals_agree():
    # 0.1.1 shipped reporting itself as 0.1.0, because the version lives both
    # in pyproject and in __init__ and only one of them was bumped. A tool
    # about versions getting its own wrong is not a small thing.
    import re

    import depdesk

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    assert depdesk.__version__ == declared


# 0.2.0: o --fail-in passou a decidir o que derruba o build.

def test_deprecation_outside_the_window_does_not_fail_the_build(tmp_path):
    # Up to 0.1.4 this exited 1, and CI fails on any non zero code, so a model
    # retiring in five months broke the build exactly like one retiring
    # tomorrow and --fail-in decided nothing.
    _write(tmp_path, "a.py", 'M = "gpt-image-1.5"\n')  # retires 2026-12-01, 72 days out
    report = _report(tmp_path, fail_in=30)
    assert report.findings[0].severity == "deprecated"
    assert report.exit_code() == 0
    assert "the build passes" in render_text(report)


def test_strict_fails_on_warnings_but_never_softens_a_failure(tmp_path):
    _write(tmp_path, "a.py", 'M = "gpt-image-1.5"\n')
    assert _report(tmp_path, fail_in=30, strict=True).exit_code() == 1
    _write(tmp_path, "b.py", 'M = "claude-opus-4-1-20250805"\n')
    assert _report(tmp_path, fail_in=30, strict=True).exit_code() == 2


def test_cli_strict_flag(tmp_path, capsys):
    _write(tmp_path, "a.py", 'M = "gpt-image-1.5"\n')
    args = ["check", str(tmp_path), "--fail-in", "30", "--today", "2026-09-20"]
    assert main(args) == 0
    assert main(args + ["--strict"]) == 1
    capsys.readouterr()


def test_json_says_what_fails_the_build(tmp_path):
    _write(tmp_path, "a.py", 'A = "gpt-image-1.5"\nB = "claude-opus-4-1-20250805"\n')
    payload = json.loads(render_json(_report(tmp_path, fail_in=30)))
    verdicts = {f["identifier"]: f["fails_build"] for f in payload["findings"]}
    assert verdicts == {"claude-opus-4-1-20250805": True, "gpt-image-1.5": False}
    assert payload["fail_in"] == 30 and payload["strict"] is False


# 0.2.0: ignore com validade.

def test_ignore_until_holds_before_the_date_and_lapses_on_it():
    line = 'M = "claude-opus-4-1-20250805"  # depdesk: ignore until=2026-12-01'
    assert is_ignored(line, date(2026, 11, 30)) is True
    assert is_ignored(line, date(2026, 12, 1)) is False
    assert is_ignored('M = "x"  # depdesk: ignore', date(2099, 1, 1)) is True


def test_ignore_until_with_a_bad_date_silences_nothing():
    # A typo in the date must not become the permanent exception the date was
    # written to prevent.
    today = date(2026, 9, 20)
    assert is_ignored("x  # depdesk: ignore until=2026-13-01", today) is False
    assert is_ignored("x  # depdesk: ignore until=next-week", today) is False
    assert is_ignored("x  # depdesk: ignore until=", today) is False


def test_ignore_until_inside_a_markdown_comment(tmp_path):
    _write(
        tmp_path,
        "notes.md",
        'Still on "claude-opus-4-1-20250805".  <!-- depdesk: ignore until=2026-12-01-->\n',
    )
    assert _report(tmp_path).findings == []
    expired = _report(tmp_path, today=date(2026, 12, 1))
    assert [f.identifier for f in expired.findings] == ["claude-opus-4-1-20250805"]


def test_ignore_until_follows_the_today_flag(tmp_path, capsys):
    # `--today` answers "what breaks later", and an ignore that will have
    # expired by then is part of that answer.
    _write(
        tmp_path,
        "a.py",
        'M = "claude-opus-4-1-20250805"  # depdesk: ignore until=2026-12-01\n',
    )
    assert main(["check", str(tmp_path), "--today", "2026-09-20"]) == 0
    assert main(["check", str(tmp_path), "--today", "2026-12-01"]) == 2
    capsys.readouterr()


def test_ignore_until_applies_to_parameters_too(tmp_path):
    _write(
        tmp_path,
        "a.py",
        'M = "claude-opus-5"\n'
        "client.messages.create(model=M, temperature=0)  # depdesk: ignore until=2026-10-01\n",
    )
    assert _report(tmp_path).param_hits == []
    assert len(_report(tmp_path, today=date(2026, 10, 1)).param_hits) == 1


# 0.2.0: anotacoes e resumo no GitHub Actions.

def test_github_is_only_on_inside_actions():
    assert github.enabled({"GITHUB_ACTIONS": "true"}) is True
    assert github.enabled({}) is False
    assert github.enabled({"GITHUB_ACTIONS": "false"}) is False


def test_github_annotations_level_follows_the_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _write(tmp_path, "app/a.py", 'A = "gpt-image-1.5"\nB = "claude-opus-4-1-20250805"\nC = "claude-quasar-9"\n')
    commands = github.annotations(_report(tmp_path, fail_in=30))
    assert len(commands) == 2  # the unknown identifier stays in the log
    retired = next(c for c in commands if "claude-opus-4-1" in c)
    assert retired.startswith("::error file=app/a.py,line=2,title=depdesk%3A already retired::")
    assert "Replacement: claude-opus-4-8." in retired
    warned = next(c for c in commands if "gpt-image-1.5" in c)
    assert warned.startswith("::warning file=app/a.py,line=1,")

    strict = github.annotations(_report(tmp_path, fail_in=30, strict=True))
    assert all(c.startswith("::error") for c in strict)


def test_github_annotation_escapes_what_would_break_the_command():
    command = github._command("error", "a: b, c", "50% of\ncalls")
    assert command == "::error title=a%3A b%2C c::50%25 of%0Acalls"


def test_github_summary_is_written_and_json_stays_clean(tmp_path, monkeypatch, capsys):
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _write(tmp_path, "src/a.py", 'M = "claude-opus-4-1-20250805"\n')

    assert main(["check", str(tmp_path / "src"), "--today", "2026-09-20"]) == 2
    out = capsys.readouterr().out
    assert "::error file=src/a.py,line=1," in out
    text = target.read_text()
    assert "| fails | `claude-opus-4-1-20250805` |" in text
    assert "Exit code 2: the build fails." in text

    main(["check", str(tmp_path / "src"), "--json", "--today", "2026-09-20"])
    json.loads(capsys.readouterr().out)  # would raise if an annotation leaked in

    target.unlink()
    main(["check", str(tmp_path / "src"), "--no-github", "--today", "2026-09-20"])
    assert "::error" not in capsys.readouterr().out
    assert not target.exists()


def test_short_aliases_do_not_match_inside_longer_ids(tmp_path):
    # gpt-4 and o1 entered the catalog in 0.2.0, when the OpenAI page started
    # listing aliases next to each snapshot. Short ids are the ones that would
    # light up inside every newer model name if the boundaries were loose.
    _write(tmp_path, "a.py", 'A = "gpt-4o"\nB = "gpt-4.1"\nC = "gpt-4-turbo-preview"\nD = "o1-mini"\n')
    ids = {f.identifier for f in _report(tmp_path).findings}
    assert "gpt-4" not in ids and "o1" not in ids
    _write(tmp_path, "b.py", 'M = "gpt-4"\n')
    finding = next(f for f in _report(tmp_path).findings if f.identifier == "gpt-4")
    assert finding.severity == "due" and finding.replacement == "gpt-5.6-sol"


# 0.2.1: dois falsos "tudo limpo" achados por revisao externa do scan.py.

def test_skip_dirs_only_apply_below_the_root(tmp_path, capsys):
    # A root under a directory called build scanned zero files and printed
    # "Nothing deprecated found", exit 0, whether the path was absolute or
    # relative. Docker images built under /build hit exactly this.
    _write(tmp_path, "build/proj/x.py", 'M = "claude-2.0"\n')
    _write(tmp_path, "build/proj/node_modules/y.js", 'const m = "claude-2.0";\n')
    report = _report(tmp_path / "build" / "proj")
    assert report.scanned == 1  # node_modules below the root is still skipped
    assert [f.identifier for f in report.findings] == ["claude-2.0"]
    assert main(["check", str(tmp_path / "build" / "proj")]) == 2
    capsys.readouterr()


def test_a_directory_with_nothing_readable_is_not_a_pass(tmp_path, capsys):
    _write(tmp_path, "only/logo.png", "not really a png\n")
    assert main(["check", str(tmp_path / "only")]) == 3
    assert "nothing was checked" in capsys.readouterr().err


def test_a_single_file_that_is_skipped_is_not_an_error(tmp_path, capsys):
    # pre-commit passes staged files one by one; a staged catalog copy is
    # skipped, and blocking the commit for that would be wrong.
    copy = json.loads(catalog_mod.load().path.read_text(encoding="utf-8"))
    path = _write(tmp_path, "catalog.json", json.dumps(copy))
    assert main(["check", str(path)]) == 0
    assert "nothing to check" in capsys.readouterr().out


def test_prefixed_catalog_id_is_reported_as_unknown_not_dated(tmp_path):
    # anthropic.claude-2.0 and azure.gpt-4 used to produce nothing at all. They
    # are not the catalog entry either: a cloud platform runs its own schedule.
    _write(tmp_path, "a.py", 'A = "anthropic.claude-2.0"\nB = "azure.gpt-4"\nC = "prod-gpt-4-turbo"\n')
    report = _report(tmp_path)
    by_id = {f.identifier: f for f in report.findings}
    assert set(by_id) == {"anthropic.claude-2.0", "azure.gpt-4", "prod-gpt-4-turbo"}
    assert all(f.severity == "unknown" for f in by_id.values())
    assert "claude-2.0" in by_id["anthropic.claude-2.0"].detail
    assert report.exit_code() == 0


def test_prefix_handling_keeps_the_old_boundaries(tmp_path):
    _write(tmp_path, "a.py", 'A = "claude-2.0"\nB = "gpt-4o"\nC = "foo1 echo1"\nD = "openai/gpt-4"\n')
    ids = sorted(f.identifier for f in _report(tmp_path).findings)
    assert ids == ["claude-2.0", "gpt-4", "gpt-4o"]


# 0.2.1: auditoria completa. Cada teste reproduz um defeito achado rodando a
# ferramenta, e o nome diz o que passou a ser verdade.

import os  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

from depdesk import scan as scan_mod  # noqa: E402
from depdesk import upstream as upstream_mod  # noqa: E402


def _ids(tmp_path, **kwargs):
    return sorted(f.identifier for f in _report(tmp_path, **kwargs).findings)


def test_a_latin1_file_is_read(tmp_path):
    (tmp_path / ".env").write_bytes(b"# configura\xe7\xe3o\nMODEL=claude-opus-4-1-20250805\n")
    assert _ids(tmp_path) == ["claude-opus-4-1-20250805"]


def test_a_utf16_file_is_read(tmp_path):
    (tmp_path / "config.yaml").write_bytes('model: "claude-3-opus-20240229"\n'.encode("utf-16"))
    assert _ids(tmp_path) == ["claude-3-opus-20240229"]


def test_a_file_over_the_old_2mb_limit_is_read(tmp_path):
    _write(tmp_path, "prompts.yaml", 'model: "claude-3-opus-20240229"\n' + "x: y\n" * 600_000)
    assert _ids(tmp_path) == ["claude-3-opus-20240229"]


def test_a_file_that_cannot_be_read_is_listed_and_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scan_mod, "MAX_BYTES", 10)
    _write(tmp_path, "big.py", 'M = "claude-3-opus-20240229"\n')
    _write(tmp_path, "ok.py", "x = 1\n")
    report = _report(tmp_path)
    assert [str(p.name) for p, _ in report.skipped] == ["big.py"]
    assert report.exit_code() == 0 and _report(tmp_path, strict=True).exit_code() == 1
    text = render_text(report)
    assert "COULD NOT READ" in text and "Nothing deprecated found. Every" not in text
    assert json.loads(render_json(report))["skipped"][0]["path"].endswith("big.py")


def test_a_binary_without_extension_is_skipped_quietly(tmp_path):
    (tmp_path / "tool").write_bytes(b"\x7fELF\x00\x00claude-2.0")
    _write(tmp_path, "a.py", "x = 1\n")
    report = _report(tmp_path)
    assert report.findings == [] and report.skipped == []


@pytest.mark.parametrize("name,body", [
    ("request.json", '{"model": "claude-opus-4-7", "temperature": 0.2}\n'),
    ("client.ts", 'create({ "model": "claude-opus-4-7", "temperature": 0 })\n'),
    ("call.sh", "curl -d '{\"model\":\"claude-opus-4-7\",\"top_p\":0.9}'\n"),
])
def test_parameters_in_quoted_and_camel_case_forms(tmp_path, name, body):
    _write(tmp_path, name, body)
    report = _report(tmp_path)
    assert report.param_hits and report.exit_code() == 2


def test_parameters_passed_through_a_dict_in_python(tmp_path):
    _write(tmp_path, "a.py", 'params = {"model": "claude-opus-4-7", "temperature": 0}\nclient.messages.create(**params)\n')
    assert _report(tmp_path).exit_code() == 2


@pytest.mark.parametrize("name,body", [
    # Pinecone's topK, in a RAG file that also calls Claude
    ("rag.ts", 'const m = "claude-opus-4-7";\nindex.query({ vector: v, topK: 5 })\n'),
    ("a.py", 'M = "claude-opus-4-7"\nkwargs = {}\nkwargs["top_p"] = 0.9\n'),
])
def test_ambiguous_parameter_forms_are_for_review(tmp_path, name, body):
    _write(tmp_path, name, body)
    report = _report(tmp_path)
    assert report.param_hits and not report.param_hits[0].certain
    assert report.exit_code() == 0 and _report(tmp_path, strict=True).exit_code() == 1


def test_a_tool_schema_is_not_a_request(tmp_path):
    _write(tmp_path, "tools.py", 'M = "claude-opus-4-7"\nTOOL = {"name": "w", "input_schema": {"properties": {"temperature": {"type": "number"}}}}\n')
    _write(tmp_path, "tools.json", '{"model": "claude-opus-4-7", "properties": {"temperature": {"type": "number"}}}\n')
    assert _report(tmp_path).param_hits == []


def test_a_bom_or_an_unparseable_python_file_still_gets_parameter_rules(tmp_path):
    (tmp_path / "bom.py").write_bytes(
        b'\xef\xbb\xbfMODEL = "claude-opus-4-7"\nclient.create(model=MODEL, temperature=0)\n'
    )
    _write(tmp_path, "future.py", 'M = "claude-opus-4-7"\nclient.create(model=M, temperature=0)\nprint "py2"\n')
    assert {h.path.name for h in _report(tmp_path).param_hits} == {"bom.py", "future.py"}


def test_line_numbers_survive_a_form_feed(tmp_path):
    _write(
        tmp_path,
        "app.py",
        'import anthropic\n\x0c\nMODEL = "claude-opus-4-7"\nLEGACY = "claude-2.0"  # depdesk: ignore\n'
        "client.messages.create(model=MODEL, temperature=0)\n",
    )
    report = _report(tmp_path)
    assert [f.identifier for f in report.findings] == []
    assert [(h.parameter, h.line) for h in report.param_hits] == [("temperature", 5)]
    assert "temperature=0" in report.param_hits[0].excerpt


@pytest.mark.parametrize("pragma", [
    "# depdesk: ignore until 2026-12-01",
    "# depdesk: ignore until:2026-12-01",
    "# depdesk: ignore-until=2026-12-01",
    "# depdesk: ignored, fix before release",
])
def test_a_mistyped_pragma_silences_nothing(tmp_path, pragma):
    _write(tmp_path, "a.py", f'M = "claude-2.0"  {pragma}\n')
    assert _ids(tmp_path) == ["claude-2.0"]


def test_the_well_formed_pragmas_still_work(tmp_path):
    _write(tmp_path, "a.py", 'A = "claude-2.0"  # depdesk: ignore\nB = "claude-2.1"  # depdesk: ignore UNTIL=2026-12-01\n')
    _write(tmp_path, "b.md", 'Old: "claude-2.0" <!-- depdesk: ignore -->\n')
    assert _ids(tmp_path) == []


def test_bare_o1_needs_to_look_like_a_model_name(tmp_path):
    _write(tmp_path, "t.py", "def overlap(o1, o2):\n    return o1 == o2\n")
    assert _ids(tmp_path) == []
    _write(tmp_path, "m.py", 'M = "o1"\n')
    _write(tmp_path, ".env", "OPENAI_MODEL=o1\n")
    _write(tmp_path, "c.yaml", "model: o1  # the reasoning one\n")
    report = _report(tmp_path)
    assert {h.path.name for f in report.findings for h in f.locations} == {"m.py", ".env", "c.yaml"}


def test_env_directories_are_read_and_virtualenvs_are_not(tmp_path):
    _write(tmp_path, "deploy/env/production.env", "ANTHROPIC_MODEL=claude-3-opus-20240229\n")
    _write(tmp_path, "tools/pyenv/pyvenv.cfg", "home = /usr\n")
    _write(tmp_path, "tools/pyenv/lib/x.py", 'M = "claude-2.0"\n')
    assert _ids(tmp_path) == ["claude-3-opus-20240229"]


def test_extensionless_config_is_read_in_the_tree_walk(tmp_path):
    _write(tmp_path, ".envrc", "export ANTHROPIC_MODEL=claude-3-opus-20240229\n")
    _write(tmp_path, "bin/summarize", '#!/usr/bin/env python3\nM = "claude-2.0"\n')
    _write(tmp_path, "Jenkinsfile", 'env.MODEL = "claude-2.1"\n')
    assert _ids(tmp_path) == ["claude-2.0", "claude-2.1", "claude-3-opus-20240229"]


def test_exclude_globs_ignore_the_directories_above_the_root(tmp_path, monkeypatch, capsys):
    # GitHub checks a repository called docs out at .../docs/docs.
    checkout = tmp_path / "docs" / "docs"
    _write(checkout, "app.py", 'M = "claude-2.0"\n')
    _write(checkout, "docs/old.md", 'M = "claude-2.1"\n')
    monkeypatch.chdir(checkout)
    assert main(["check", ".", "--exclude", "*/docs/*", "--today", "2026-09-20", "--json"]) == 2
    found = json.loads(capsys.readouterr().out)["findings"]
    assert [f["identifier"] for f in found] == ["claude-2.0"]


def test_an_id_that_ends_a_sentence_is_found(tmp_path):
    _write(tmp_path, "notes.md", "The summarizer still calls claude-3-opus-20240229.\nNot gpt-4.1 though.\n")
    ids = _ids(tmp_path)
    assert "claude-3-opus-20240229" in ids and "gpt-4" not in ids


def test_overlapping_roots_scan_each_file_once(tmp_path):
    path = _write(tmp_path, "app/a.py", 'M = "claude-2.0"\n')
    cat = _catalog()
    result = scan([tmp_path, path, tmp_path], cat, today=TODAY)
    assert result.files_scanned == 1 and len(result.hits) == 1


def test_endpoints_and_headers_are_matched_the_way_code_writes_them(tmp_path):
    _write(
        tmp_path,
        "client.py",
        'requests.get("https://api.openai.com/v1/prompts")\n'
        'headers = {"OpenAI-Beta": "assistants=v1"}\n',
    )
    assert _ids(tmp_path) == ["/v1/prompts", "OpenAI-Beta: assistants=v1"]


def test_fine_tuned_babbage_uses_its_own_date(tmp_path):
    _write(tmp_path, "a.py", 'M = "ft:babbage-002:acme::abc123"\n')
    finding = _report(tmp_path).findings[0]
    assert finding.identifier == "ft:babbage-002"
    assert finding.entry.retires_on.isoformat() == "2026-10-23"


@pytest.mark.parametrize("body", [
    "[]",
    "null",
    '{"schema":1,"verified_on":"2026-09-24","parameters":[{"provider":"x"}]}',
    '{"schema":1,"verified_on":"2026-09-24","models":{"a":1}}',
    '{"schema":1,"verified_on":"2026-09-24","models":["a"]}',
    '{"schema":1,"verified_on":"2026-09-24","models":[{"id":["a"],"status":"active"}]}',
])
def test_a_broken_catalog_exits_3_not_with_a_traceback(tmp_path, capsys, body):
    path = _write(tmp_path, "cat.json", body)
    _write(tmp_path, "src/a.py", "x = 1\n")
    assert main(["check", str(tmp_path / "src"), "--catalog", str(path)]) == 3
    assert main(["list", "--catalog", str(path)]) == 3
    assert main(["check", str(tmp_path / "src"), "--catalog", str(tmp_path)]) == 3
    capsys.readouterr()


@pytest.mark.parametrize("args", [
    ["--fail-in", "-1"],
    ["--fail-in", "sixty"],
    ["--sunset-in", "-5"],
    ["--locations", "-1"],
    ["--today", "2027-13-01"],
    ["--stirct"],
])
def test_invocation_errors_exit_3(tmp_path, capsys, args):
    _write(tmp_path, "a.py", "x = 1\n")
    with pytest.raises(SystemExit) as exc:
        main(["check", str(tmp_path), *args])
    assert exc.value.code == 3
    capsys.readouterr()


def test_the_summary_counts_parameters(tmp_path):
    _write(tmp_path, "a.py", 'c.messages.create(model="claude-opus-5", temperature=0)\n')
    report = _report(tmp_path)
    assert "Summary: 1 deprecated parameter use(s)." in render_text(report)
    assert report.exit_code() == 2


def test_sunset_in_zero_is_off(tmp_path):
    _write(tmp_path, "a.py", 'M = "claude-opus-4-8"\n')  # earliest retirement 2027-05-28
    cat = _catalog()
    result = scan([tmp_path], cat, today=date(2027, 6, 1))
    assert build(result, cat, today=date(2027, 6, 1), fail_in=90, sunset_in=0).findings == []
    sunset = build(result, cat, today=date(2027, 6, 1), fail_in=90, sunset_in=30)
    assert "passed 4 days ago" in render_text(sunset)


def test_a_dead_replacement_points_at_the_living_one(tmp_path):
    _write(tmp_path, "a.py", 'M = "chatgpt-4o-latest"\n')
    finding = _report(tmp_path).findings[0]
    assert finding.replacement == "gpt-5.1-chat-latest"
    assert finding.replacement_note == "itself retired; its replacement is gpt-5.6-sol"


def test_locations_zero_still_annotates(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    _write(tmp_path, "a.py", 'M = "claude-2.0"\n')
    assert len(github.annotations(_report(tmp_path), limit=0)) == 1


def test_no_unknown_does_not_claim_a_section_it_hid(tmp_path):
    _write(tmp_path, "a.py", "x = 1\n")
    cat = _catalog()
    report = build(scan([tmp_path], cat, today=TODAY), cat, today=TODAY, fail_in=90, sunset_in=0,
                   usage={"claude-foo-9": 1.0}, usage_shares={"claude-foo-9": 1.0},
                   include_unknown=False)
    assert "left out because of --no-unknown" in render_text(report)


def test_output_survives_a_stdout_that_cannot_encode_it(tmp_path):
    _write(tmp_path, "a.py", 'MODEL = "claude-opus-4-1-20250805"  # → migrate \U0001f680\n')
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONPATH=str(Path(__file__).resolve().parent.parent))
    done = subprocess.run(
        [sys.executable, "-m", "depdesk", "check", str(tmp_path), "--no-github"],
        env=env, capture_output=True,
    )
    assert done.returncode == 2 and b"Traceback" not in done.stderr


@pytest.mark.parametrize("csv_text,expected", [
    ("date,api_key_name,model,output_tokens\nd,prod,claude-2.0,900\nd,prod,claude-2.1,100\n", 0.9),
    ("usage_date_utc,model_version,uncached_input_tokens\nd,claude-2.0,900\nd,claude-2.1,100\n", 0.9),
    ("model;requests\nclaude-2.0;300\nclaude-2.1;100\n", 0.75),
    ("﻿model,requests\nclaude-2.0,3\nclaude-2.1,1\n", 0.75),
    ("model,requests\nclaude-2.0,NaN\nclaude-2.0,-5\nclaude-2.0,9\nclaude-2.1,1\n", 0.9),
])
def test_usage_columns_are_chosen_by_what_they_hold(tmp_path, csv_text, expected):
    path = _write(tmp_path, "u.csv", csv_text)
    totals, _ = load_usage(path)
    assert share(totals)["claude-2.0"] == pytest.approx(expected)


def test_usage_json_rows_use_the_same_hints(tmp_path):
    path = _write(tmp_path, "u.json", json.dumps({"data": [
        {"model": "claude-2.0", "input_tokens": 900}, {"model": "claude-2.1", "input_tokens": 100},
    ]}))
    totals, column = load_usage(path)
    assert column == "input_tokens" and share(totals)["claude-2.0"] == 0.9


def test_usage_errors_instead_of_silent_zeros(tmp_path):
    from depdesk.usage import UsageError
    path = _write(tmp_path, "u.csv", "model,requests\nclaude-2.0,0\n")
    with pytest.raises(UsageError):
        load_usage(path)
    path = _write(tmp_path, "v.csv", "model,input_tokens\nclaude-2.0,5\n")
    with pytest.raises(UsageError):
        load_usage(path, count_column="inputtokens")


PAGE = b"""<html><nav>Recent: Bringing GPT-Live-1 to life</nav>
<main>As we launch safer models... <table>
<tr><td>Aug 10, 2026</td><td>gpt-5.2-chat-latest</td></tr>
<tr><td>October 23, 2026</td><td>o1-2024-12-17</td></tr>
<tr><td>December 11, 2026</td><td>o3-2025-04-16</td></tr>
<tr><td>2026\xe2\x80\x9108\xe2\x80\x9126</td><td>Assistants API</td></tr>
</table></main>Ask AI</html>"""
BETWEEN = ["As we launch", "Ask AI"]


@pytest.mark.parametrize("old,new", [
    (b"Aug 10, 2026", b"Sep 30, 2026"),
    (b"2026\xe2\x80\x9108\xe2\x80\x9126", b"2026\xe2\x80\x9112\xe2\x80\x9126"),
    # a row moving to a date the page already names elsewhere
    (b"October 23, 2026</td><td>o1", b"December 11, 2026</td><td>o1"),
])
def test_upstream_sees_every_date_change(old, new):
    assert upstream_mod.fingerprint(PAGE, BETWEEN) != upstream_mod.fingerprint(PAGE.replace(old, new), BETWEEN)


def test_upstream_ignores_the_navigation():
    moved = PAGE.replace(b"GPT-Live-1", b"GPT-Live-2")
    assert upstream_mod.fingerprint(PAGE, BETWEEN) == upstream_mod.fingerprint(moved, BETWEEN)


def test_upstream_treats_an_empty_page_or_a_crash_as_could_not_check(monkeypatch, tmp_path, capsys):
    cat = _catalog()
    monkeypatch.setattr(upstream_mod, "fetch", lambda url: b"<html><div id=root></div></html>")
    assert all(not r.ok for r in upstream_mod.check(cat))

    def explode(url):
        raise ValueError("unknown url type")
    monkeypatch.setattr(upstream_mod, "fetch", explode)
    results = upstream_mod.check(cat)
    assert all(not r.ok for r in results) and exit_code(results) == 3


def test_upstream_save_reports_a_source_it_could_not_fetch(monkeypatch, tmp_path, capsys):
    copy = _write(tmp_path, "catalog.json", catalog_mod.load().path.read_text(encoding="utf-8"))
    real = PAGE.replace(b"As we launch", b"As we launch As safer and more capable models launch")

    def half(url):
        if "openai" in url:
            raise OSError("timed out")
        return real + b" Was this page helpful"
    monkeypatch.setattr(upstream_mod, "fetch", half)
    assert main(["upstream", "--catalog", str(copy), "--save"]) == 3
    capsys.readouterr()


def test_the_shipped_catalog_agrees_with_itself():
    cat = _catalog()
    index = cat.by_id
    for entry in cat.entries:
        if entry.status == "deprecated" and entry.retires_on:
            assert entry.retires_on >= cat.verified_on, f"{entry.id} is past its date but still deprecated"
        if entry.status == "retired" and entry.retires_on:
            assert entry.retires_on <= cat.verified_on, f"{entry.id} is retired with a future date"
        if entry.replacement and " " not in entry.replacement and entry.kind == "model":
            assert entry.replacement not in index or index[entry.replacement].id, entry.id


def _readme_block(after: str) -> list:
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    start = readme.index(after) + len(after)
    return readme[start:readme.index("```", start)].splitlines()


def test_the_readme_sample_is_what_the_tool_prints(monkeypatch, capsys):
    # "Every line of that output is real" was true on the day it was written
    # and then drifted: wrong line numbers, a summary one finding short. The
    # block is now checked against the tool on the date it was captured.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(Path(__file__).resolve().parent.parent / "examples" / "legacy-app")
    assert main(["check", "app", "--fail-in", "60", "--today", "2026-09-26", "--no-github"]) == 2
    printed = capsys.readouterr().out.splitlines()
    block = _readme_block("$ depdesk check app --fail-in 60\n")
    assert block[: block.index("$ echo $?")] == printed

    main(["check", "app", "--usage", "usage.csv", "--today", "2026-09-26", "--no-github"])
    printed = capsys.readouterr().out.splitlines()
    excerpt = [line for line in _readme_block("```\nALREADY RETIRED\n") if line]
    remaining = iter(printed)
    assert all(any(line == seen for seen in remaining) for line in excerpt)


def test_products_are_still_found_in_prose(tmp_path):
    # The bare id rule for o1 briefly swallowed every id without a hyphen,  # depdesk: ignore
    # products included, which a scan of a real repository caught.
    _write(tmp_path, "notes.md", "We still build flows in Agent Builder and the Assistants API.\n")
    assert _ids(tmp_path) == ["Agent Builder", "Assistants API"]


def test_an_unlistable_directory_is_reported_not_a_crash(tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root can list anything")
    _write(tmp_path, "ok.py", 'M = "claude-2.0"\n')
    locked = tmp_path / "pgdata"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert main(["check", str(tmp_path), "--no-github"]) == 2
        assert "directory could not be listed" in capsys.readouterr().out
    finally:
        locked.chmod(0o755)


def test_generic_rest_paths_only_count_on_an_openai_line(tmp_path):
    _write(tmp_path, "a.py", 'S = "https://api.spotify.com/v1/search"\nR = "/api/v1/prompts"\n')
    assert _ids(tmp_path) == []
    _write(tmp_path, "b.py", 'client = OpenAI(); client.get("/v1/prompts")\nh["OpenAI-Beta"] = "assistants=v1"\n')
    assert _ids(tmp_path) == ["/v1/prompts", "OpenAI-Beta: assistants=v1"]


def test_large_binaries_are_not_unread_text(tmp_path, monkeypatch):
    monkeypatch.setattr(scan_mod, "MAX_BYTES", 100)
    (tmp_path / "weights.onnx").write_bytes(b"\x00" * 500)
    (tmp_path / "server").write_bytes(b"\x7fELF\x00" + b"\x00" * 500)
    (tmp_path / "blob.dat").write_bytes(b"\x00" * 500)
    _write(tmp_path, "a.py", "x = 1\n")
    report = _report(tmp_path, strict=True)
    assert report.skipped == [] and report.exit_code() == 0


def test_a_source_file_with_a_nul_is_still_read(tmp_path):
    (tmp_path / "split.py").write_bytes(b'SEP = "\x00"\nMODEL = "claude-2.0"\n')
    assert _ids(tmp_path) == ["claude-2.0"]


def test_prefix_checks_are_not_uses_of_the_shorter_model(tmp_path):
    _write(tmp_path, "router.py", 'if model.startswith("gpt-4."):\n    pass\nfnmatch(model, "gpt-4.*")\n')
    assert "gpt-4" not in _ids(tmp_path)


@pytest.mark.parametrize("name,body", [
    ("docker-compose.yml", "services:\n  app:\n    environment:\n      - MODEL=o1\n"),
    ("Dockerfile", "FROM python\nENV MODEL=o1\n"),
    ("ci.yml", "matrix:\n  model: [o1, gpt-4o]\n"),
    ("models.yaml", "models:\n  - o1\n"),
    ("run.sh", "python app.py --model o1\n"),
])
def test_bare_o1_in_config_forms(tmp_path, name, body):
    _write(tmp_path, name, body)
    assert "o1" in _ids(tmp_path)


def test_bare_o1_as_a_variable_is_not_a_model(tmp_path):
    _write(tmp_path, "geometry.py", "def overlap(o1, o2):\n    first = o1\n    return f(left=o1, right=o2)\n")
    assert _ids(tmp_path) == []


def test_exclude_patterns_written_from_the_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "src/generated/client.py", 'M = "claude-2.0"\n')
    _write(tmp_path, "src/app/main.py", "x = 1\n")
    for pattern in ("src/generated/*", "generated/*"):
        assert main(["check", "src", "--exclude", pattern, "--no-github"]) == 0
        assert main(["check", "src/generated/client.py", "src/app/main.py", "--exclude", pattern, "--no-github"]) == 0
    capsys.readouterr()


def test_usage_only_findings_show_when_no_file_was_read(tmp_path, capsys):
    path = _write(tmp_path, "logo.png", "x")
    usage = _write(tmp_path, "u.csv", "model,requests\nclaude-2.0,9\n")
    assert main(["check", str(path), "--usage", str(usage), "--no-github"]) == 2
    assert "claude-2.0" in capsys.readouterr().out


def test_european_numbers_in_a_semicolon_csv(tmp_path):
    path = _write(tmp_path, "u.csv", "model;requests\nclaude-2.0;1.420\nclaude-2.1;0,50\n")
    totals, _ = load_usage(path)
    assert totals == {"claude-2.0": 1420.0, "claude-2.1": 0.5}


def test_usage_envelope_with_more_keys_and_bad_values(tmp_path):
    path = _write(tmp_path, "u.json", json.dumps({"object": "list", "has_more": False,
                                                 "data": [{"model": "claude-2.0", "requests": 5}]}))
    assert load_usage(path)[0] == {"claude-2.0": 5.0}
    path = _write(tmp_path, "v.json", '{"claude-2.0": 120, "claude-opus-5": NaN, "x": -3}')
    assert load_usage(path)[0]["claude-2.0"] == 120.0


def test_a_malformed_between_is_a_catalog_error(tmp_path, capsys):
    raw = json.loads(catalog_mod.load().path.read_text(encoding="utf-8"))
    raw["sources"][0]["between"] = ["only one"]
    path = _write(tmp_path, "cat.json", json.dumps(raw))
    assert main(["upstream", "--catalog", str(path)]) == 3
    capsys.readouterr()


def test_a_marker_that_repeats_is_not_trusted():
    text = "nav As we launch ... rows ... Try Ask AI ... more rows ... Ask AI footer"
    assert upstream_mod.article(text, BETWEEN) == (text, False)


def test_an_embedded_token_is_listed_once(tmp_path):
    _write(tmp_path, "a.py", 'RUN = "gemini-pro-vs-gpt-4"\n')
    report = _report(tmp_path)
    assert [len(f.locations) for f in report.findings] == [1] * len(report.findings)
