"""Security-focused regression tests for jev_mcp.py.

Locks in the XSS fix (no innerHTML, scheme-guarded hrefs), env-hijack
protection (load_env never overwrites process values), ledger robustness
(corrupt JSONL never raises), and secret hygiene (stats output never
echoes API keys). All tests are offline, fast, and touch only tmp_path.
"""

import json
import re

import jev_mcp

# --- dashboard XSS regressions -------------------------------------------


def test_dashboard_never_uses_innerhtml_and_uses_textcontent():
    """Regression guard: rows were built with innerHTML from untrusted page URLs."""
    assert "innerHTML" not in jev_mcp.DASHBOARD
    assert "textContent" in jev_mcp.DASHBOARD
    assert "replaceChildren" in jev_mcp.DASHBOARD  # safe DOM API renders the rows


def test_dashboard_href_assignment_is_scheme_guarded():
    """The only href assignment must sit inside the https?:// guard."""
    html = jev_mcp.DASHBOARD
    assert "a.href=u" in html
    assert "if(/^https?:\\/\\//i.test(u)){a.href=u" in html


def test_dashboard_href_guard_rejects_javascript_urls():
    """The guard regex shipped in the dashboard must reject non-http schemes."""
    match = re.search(r"/(\^https\?:\\/\\/)/i", jev_mcp.DASHBOARD)
    assert match, "scheme guard regex missing from dashboard"
    guard = re.compile(match.group(1), re.IGNORECASE)
    assert guard.match("https://example.com")
    assert guard.match("http://example.com")
    assert not guard.match("javascript:alert(1)")
    assert not guard.match("data:text/html,<script>alert(1)</script>")
    assert not guard.match("vbscript:msgbox(1)")
    assert not guard.match("//example.com")  # protocol-relative must not pass


def test_serve_binds_loopback_only():
    """The dashboard must never listen on a non-loopback interface."""
    server = jev_mcp.build_server(0)
    try:
        host = server.server_address[0]
        assert host in ("127.0.0.1", "::1"), host
        assert host != "0.0.0.0"
    finally:
        server.server_close()


# --- load_env: a stray .env must not hijack real credentials -------------


def test_load_env_does_not_overwrite_existing_secret(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-from-process")
    jev_mcp.load_env(env_file)
    assert jev_mcp.os.environ["TYPESAFE_API_KEY"] == "sk-from-process"


def test_load_env_missing_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_TEST_SENTINEL", "keep-me")
    jev_mcp.load_env(tmp_path / "absent.env")  # must not raise
    assert jev_mcp.os.environ["JEV_TEST_SENTINEL"] == "keep-me"


def test_load_env_ignores_malformed_lines(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nNO_EQUALS_HERE\nTYPESAFE_API_KEY=sk-ok\n", encoding="utf-8"
    )
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("NO_EQUALS_HERE", raising=False)
    jev_mcp.load_env(env_file)
    assert jev_mcp.os.environ.get("TYPESAFE_API_KEY") == "sk-ok"
    assert "NO_EQUALS_HERE" not in jev_mcp.os.environ


# --- read_ledger: hostile JSONL must never raise -------------------------


def test_read_ledger_skips_corrupt_and_blank_lines(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text(
        '{"ts": "2026-01-01T00:00:00+00:00", "status": "done"}\n'
        "\n"
        "   \n"
        "not json at all\n"
        '{"broken": \n'
        '{"ts": "2026-01-02T00:00:00+00:00", "status": "blocked"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(jev_mcp, "LEDGER", ledger)
    rows = jev_mcp.read_ledger()
    assert [r["status"] for r in rows] == ["done", "blocked"]


def test_read_ledger_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_mcp, "LEDGER", tmp_path / "absent.jsonl")
    assert jev_mcp.read_ledger() == []


def test_read_ledger_never_raises_on_hostile_content(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text("{\nunterminated\n\x00\x01\x02\n\n", encoding="utf-8")
    monkeypatch.setattr(jev_mcp, "LEDGER", ledger)
    assert jev_mcp.read_ledger() == []


# --- secret hygiene: stats output must never echo keys -------------------


def test_format_stats_never_emits_planted_secret():
    secret = "sk-planted-secret-9f8e7d"
    rows = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "status": "done",
            "elapsed_ms": 100,
            "goal": "find flights",
            "est_cost_usd": 0.001,
            "api_key": secret,
            "TYPESAFE_API_KEY": secret,
            "TEXT_MODEL_API_KEY": secret,
            "final_url": secret,
        }
    ]
    out = jev_mcp.format_stats(jev_mcp.aggregate(rows), rows)
    assert secret not in out


def test_counter_summary_never_emits_planted_secret():
    secret = "sk-planted-secret-9f8e7d"
    rows = [
        {
            "elapsed_ms": 100,
            "typesafe_input_tokens": 10,
            "est_cost_usd": 0.001,
            "api_key": secret,
            "TEXT_MODEL_API_KEY": secret,
        }
    ]
    counter = jev_mcp.counter_summary(rows)
    assert secret not in json.dumps(counter)


def test_stats_functions_do_not_read_api_key_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-env-sentinel-123")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "sk-env-sentinel-456")
    out = jev_mcp.format_stats(jev_mcp.aggregate([]), [])
    assert "sk-env-sentinel-123" not in out
    assert "sk-env-sentinel-456" not in out
    counter = jev_mcp.counter_summary([])
    assert "sk-env-sentinel-123" not in json.dumps(counter)
    assert "sk-env-sentinel-456" not in json.dumps(counter)


# --- cost math robustness ------------------------------------------------


def test_estimate_cost_empty_usage_does_not_raise():
    cost, note = jev_mcp.estimate_cost({})
    assert cost == 0.0
    assert isinstance(note, str)


def test_price_rejects_unparseable_values(monkeypatch):
    for bad in ("not-a-number", "1,5", "12abc", "0x10"):
        monkeypatch.setenv("JEV_TEXT_PRICE_PROMPT_PER_M", bad)
        assert jev_mcp._price("JEV_TEXT_PRICE_PROMPT_PER_M", 0.75) == 0.75