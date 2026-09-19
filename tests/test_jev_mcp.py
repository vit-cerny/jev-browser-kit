"""Unit tests for the MCP wrapper's pure logic: no network, no browser, no paid APIs."""

import pytest

import jev_mcp


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "JEV_CHROME",
        "JEV_TYPESAFE_PRICE_PER_MTOK_INPUT",
        "JEV_TEXT_PRICE_PROMPT_PER_M",
        "JEV_TEXT_PRICE_COMPLETION_PER_M",
    ):
        monkeypatch.delenv(name, raising=False)


def test_price_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("JEV_TEXT_PRICE_PROMPT_PER_M", raising=False)
    assert jev_mcp._price("JEV_TEXT_PRICE_PROMPT_PER_M", 1.5) == 1.5


def test_price_uses_default_when_unparseable(monkeypatch):
    monkeypatch.setenv("JEV_TEXT_PRICE_PROMPT_PER_M", "not-a-number")
    assert jev_mcp._price("JEV_TEXT_PRICE_PROMPT_PER_M", 1.5) == 1.5


def test_price_reads_a_real_value(monkeypatch):
    monkeypatch.setenv("JEV_TEXT_PRICE_PROMPT_PER_M", "2.5")
    assert jev_mcp._price("JEV_TEXT_PRICE_PROMPT_PER_M", 0.0) == 2.5


def test_estimate_cost_bills_typesafe_input_at_published_rate():
    usage = {"typesafe_input_tokens": 1_000_000, "prompt_tokens": 0, "completion_tokens": 0}
    cost, note = jev_mcp.estimate_cost(usage)
    assert cost == pytest.approx(0.042)
    assert "output free" in note


def test_estimate_cost_without_tokens_is_zero():
    cost, _ = jev_mcp.estimate_cost(
        {"typesafe_input_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    )
    assert cost == 0.0


def test_estimate_cost_includes_configured_text_price(monkeypatch):
    monkeypatch.setenv("JEV_TEXT_PRICE_PROMPT_PER_M", "1.0")
    usage = {"typesafe_input_tokens": 0, "prompt_tokens": 1_000_000, "completion_tokens": 0}
    cost, _ = jev_mcp.estimate_cost(usage)
    assert cost == pytest.approx(1.0)


def test_aggregate_totals_and_average():
    rows = [
        {"elapsed_ms": 1000, "decisions": 3, "typesafe_input_tokens": 100, "prompt_tokens": 10,
         "completion_tokens": 5, "browser_actions": 2, "text_calls": 1, "status": "done",
         "est_cost_usd": 0.001},
        {"elapsed_ms": 3000, "decisions": 5, "typesafe_input_tokens": 200, "prompt_tokens": 20,
         "completion_tokens": 10, "browser_actions": 4, "text_calls": 2, "status": "blocked",
         "est_cost_usd": 0.002},
    ]
    totals = jev_mcp.aggregate(rows)
    assert totals["searches"] == 2
    assert totals["total_elapsed_ms"] == 4000
    assert totals["avg_elapsed_ms"] == 2000
    assert totals["total_decisions"] == 8
    assert totals["total_typesafe_input_tokens"] == 300
    assert totals["total_tokens"] == 45
    assert totals["total_cost_usd"] == pytest.approx(0.003)
    assert totals["statuses"] == {"done": 1, "blocked": 1}


def test_aggregate_on_empty_ledger_does_not_divide_by_zero():
    totals = jev_mcp.aggregate([])
    assert totals["searches"] == 0
    assert totals["avg_elapsed_ms"] == 0
    assert totals["total_cost_usd"] is None


def test_read_ledger_skips_blank_and_corrupt_lines(monkeypatch, tmp_path):
    ledger = tmp_path / "usage.jsonl"
    ledger.write_text('{"a": 1}\n\nnot json\n{"b": 2}\n', encoding="utf-8")
    monkeypatch.setattr(jev_mcp, "LEDGER", ledger)
    assert jev_mcp.read_ledger() == [{"a": 1}, {"b": 2}]


def test_read_ledger_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_mcp, "LEDGER", tmp_path / "absent.jsonl")
    assert jev_mcp.read_ledger() == []


def test_find_browser_honours_override(monkeypatch, tmp_path):
    fake = tmp_path / "thorium.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("JEV_CHROME", str(fake))
    assert jev_mcp.find_browser() == str(fake)


def test_find_browser_returns_a_path_when_nothing_is_installed(monkeypatch):
    monkeypatch.setenv("JEV_CHROME", r"%NOPE%\missing.exe")
    assert jev_mcp.find_browser().endswith("missing.exe")


def test_load_env_does_not_override_process_values(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("JEV_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("JEV_TEST_KEY", "from-process")
    jev_mcp.load_env(env_file)
    assert jev_mcp.os.environ["JEV_TEST_KEY"] == "from-process"


def test_load_env_reads_values_and_ignores_comments(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# a comment\nJEV_TEST_NEW=hello\n", encoding="utf-8")
    monkeypatch.delenv("JEV_TEST_NEW", raising=False)
    jev_mcp.load_env(env_file)
    assert jev_mcp.os.environ["JEV_TEST_NEW"] == "hello"


def test_dashboard_never_uses_innerhtml():
    """Regression guard: rows were built with innerHTML from untrusted page URLs."""
    assert "innerHTML" not in jev_mcp.DASHBOARD


def test_profile_is_per_browser_and_preserves_chrome(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_CHROME_PROFILE", raising=False)
    monkeypatch.setattr(jev_mcp, "PROFILE_HOME", tmp_path)
    chrome = jev_mcp.profile_for(r"C:\x\chrome.exe")
    thorium = jev_mcp.profile_for(r"C:\x\thorium.exe")
    assert chrome.name == "jev-chrome-profile"
    assert thorium.name == "jev-thorium-profile"
    assert chrome != thorium


def test_profile_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_CHROME_PROFILE", str(tmp_path / "custom"))
    assert jev_mcp.profile_for("anything.exe") == tmp_path / "custom"


def test_counter_summary_reports_cumulative_time_and_price():
    rows = [
        {"elapsed_ms": 2000, "typesafe_input_tokens": 1000, "est_cost_usd": 0.00004},
        {"elapsed_ms": 4000, "typesafe_input_tokens": 2000, "est_cost_usd": 0.00008},
    ]
    counter = jev_mcp.counter_summary(rows)
    assert counter["searches"] == 2
    assert counter["total_elapsed_s"] == 6.0
    assert counter["avg_elapsed_ms"] == 3000
    assert counter["total_typesafe_input_tokens"] == 3000
    assert counter["total_cost_usd"] == pytest.approx(0.00012)


def test_counter_summary_on_empty_ledger():
    counter = jev_mcp.counter_summary([])
    assert counter["searches"] == 0
    assert counter["total_elapsed_s"] == 0
    assert counter["total_cost_usd"] is None


def test_redact_masks_typeafe_style_keys():
    secret = "apikey_" + "a" * 40
    out = jev_mcp.redact(f"boom {secret}")
    assert secret not in out
    assert "***" in out


def test_redact_masks_sk_github_and_aws_shapes():
    for secret in ("sk-" + "a" * 40, "ghp_" + "b" * 36, "github_pat_" + "c" * 30, "AKIA" + "D" * 16):
        assert secret not in jev_mcp.redact(f"prefix {secret} suffix")


def test_redact_leaves_ordinary_text_untouched():
    assert jev_mcp.redact("no credentials here") == "no credentials here"


def test_sandbox_profile_is_separate_from_normal_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_CHROME_PROFILE", raising=False)
    monkeypatch.delenv("JEV_SANDBOX", raising=False)
    monkeypatch.setattr(jev_mcp, "PROFILE_HOME", tmp_path)
    normal = jev_mcp.profile_for("chrome.exe", sandbox=False)
    sand = jev_mcp.profile_for("chrome.exe", sandbox=True)
    assert normal.name == "jev-chrome-profile"
    assert sand.name == "jev-chrome-sandbox-profile"
    assert normal != sand


def test_sandbox_env_var_switches_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_CHROME_PROFILE", raising=False)
    monkeypatch.setattr(jev_mcp, "PROFILE_HOME", tmp_path)
    monkeypatch.setenv("JEV_SANDBOX", "1")
    assert jev_mcp.profile_for("chrome.exe").name == "jev-chrome-sandbox-profile"


def test_safe_url_rejects_non_http_schemes():
    """file:// would read local files (including .env) straight back to the LLM."""
    for bad in ("file:///C:/Users/x/.env", "javascript:alert(1)", "chrome://settings",
                "data:text/html,hi", "ftp://example.com/x"):
        url, error = jev_mcp.safe_url(bad)
        assert url is None, bad
        assert error, bad


def test_safe_url_allows_http_https_and_empty():
    for good in ("https://example.com", "http://127.0.0.1:8767/x"):
        url, error = jev_mcp.safe_url(good)
        assert url == good and error is None
    assert jev_mcp.safe_url("") == (None, None)
    assert jev_mcp.safe_url(None) == (None, None)


def test_hide_url_secrets_drops_userinfo_and_query_values():
    hidden = jev_mcp.hide_url_secrets("https://user:pa55word@example.com/path?token=s3cret&b=2#frag")
    assert "pa55word" not in hidden
    assert "s3cret" not in hidden
    assert hidden.startswith("https://example.com/path")


def test_dashboard_rejects_non_loopback_host(monkeypatch, tmp_path):
    """DNS rebinding: a page on evil.example.com resolving to 127.0.0.1 must not read the ledger."""
    import threading
    import urllib.error
    import urllib.request

    monkeypatch.setattr(jev_mcp, "LEDGER", tmp_path / "usage.jsonl")
    server = jev_mcp.build_server(0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=5) as response:
            assert response.status == 200
        forged = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/stats", headers={"Host": "evil.example.com"}
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(forged, timeout=5)
        assert raised.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
