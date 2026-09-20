"""Tests. Run with: python3 -m pytest -q  (or python3 tests/test_depdesk.py)"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from depdesk import catalog as catalog_mod  # noqa: E402
from depdesk.__main__ import main  # noqa: E402
from depdesk.report import build, render_json, render_text  # noqa: E402
from depdesk.scan import scan  # noqa: E402
from depdesk.upstream import fingerprint  # noqa: E402
from depdesk.usage import load_usage, share  # noqa: E402

TODAY = date(2026, 9, 20)


def _catalog():
    return catalog_mod.load()


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _report(tmp_path: Path, fail_in: int = 90, **kwargs):
    cat = _catalog()
    result = scan([tmp_path], cat)
    return build(result, cat, today=TODAY, fail_in=fail_in, sunset_in=0, roots=[tmp_path], **kwargs)


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
    assert report.exit_code() == 1  # review, not failure


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
