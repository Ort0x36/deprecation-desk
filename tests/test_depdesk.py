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
    # gpt-4-turbo is in the catalog; gpt-4-turbo-preview is a different string
    # and must not be reported as if it were the known one.
    _write(tmp_path, "a.py", 'M = "gpt-4-turbo-preview"\n')
    report = _report(tmp_path)
    ids = [f.identifier for f in report.findings]
    assert "gpt-4-turbo" not in ids
    assert "gpt-4-turbo-preview" in ids
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
    assert main(["check", str(Path(catalog_mod.DEFAULT_CATALOG).parent)]) == 0
    capsys.readouterr()


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
