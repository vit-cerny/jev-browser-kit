#!/usr/bin/env python
"""Jev as an MCP tool: any LLM harness can call a real browser agent and get the result back.

Speaks MCP over stdio, so the same server works in opencode, Codex, DeepSeek Harness
and anything else that supports MCP.

CLI (for testing or terminal use):
    python jev_mcp.py --search "Find the cheapest flight from Prague to Barcelona"
    python jev_mcp.py --search "..." --url "https://www.google.com/imghp" --log
    python jev_mcp.py --stats
    python jev_mcp.py --serve          # live stats dashboard at http://127.0.0.1:8767
    python jev_mcp.py --configure      # guided setup: API keys + browser choice
    python jev_mcp.py --browsers       # list detected Chromium forks
    python jev_mcp.py --wipe-sandbox   # delete isolated sandbox profiles
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
LEDGER = ROOT / "artifacts" / "jev_usage.jsonl"
DEFAULT_URL = "https://www.google.com/search?q="
DEBUG_PORT = int(os.environ.get("JEV_DEBUG_PORT", "9222"))
PROFILE_HOME = Path(os.environ.get("JEV_CHROME_PROFILE_HOME", Path.home() / ".cache"))

# Any Chromium-based browser works: jev needs a real Blink layout engine for
# getBoundingClientRect/elementFromPoint plus a CDP endpoint. Firefox and the
# lightweight non-rendering engines cannot drive it. JEV_CHROME overrides the search.
BROWSER_FORKS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Thorium\Application\thorium.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
    r"%LOCALAPPDATA%\Programs\Opera\opera.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def find_browser():
    override = os.environ.get("JEV_CHROME")
    if override:
        return os.path.expandvars(override)
    for candidate in BROWSER_FORKS:
        path = os.path.expandvars(candidate)
        if Path(path).exists():
            return path
    return os.path.expandvars(BROWSER_FORKS[0])


def profile_for(browser, sandbox=None):
    """One profile per browser, because a Chromium fork older than the one that created a
    profile refuses to open it - sharing a directory would break the setup on switch.
    Chrome keeps the original path so an already-consented profile survives.

    sandbox=True (or JEV_SANDBOX=1) moves to a separate profile that never sees the normal
    browsing session. That is profile isolation, not an OS sandbox: wipe it with
    --wipe-sandbox, or run the browser in a container and point JEV_CHROME at it."""
    override = os.environ.get("JEV_CHROME_PROFILE")
    if override:
        return Path(override)
    if sandbox is None:
        sandbox = os.environ.get("JEV_SANDBOX") == "1"
    suffix = "-sandbox" if sandbox else ""
    return PROFILE_HOME / f"jev-{Path(browser).stem.lower()}{suffix}-profile"


def sandbox_profiles():
    return sorted(PROFILE_HOME.glob("jev-*-sandbox-profile"))


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


SECRET_PATTERNS = (
    re.compile(r"apikey_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)


def redact(text):
    """Scrub credential-shaped strings before they reach a log, a tool result or the
    user's LLM. Providers can echo an Authorization header back in an error message."""
    out = str(text)
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(lambda match: match.group(0)[:4] + "***" + match.group(0)[-2:], out)
    return out


ALLOWED_SCHEMES = ("http://", "https://")
CONTENT_CHAR_CAP = 100_000
CHROMIUM_HINTS = ("chrome", "chromium", "thorium", "brave", "edg/", "vivaldi", "opr/")


def safe_url(raw):
    """Validate an LLM-supplied start URL. Only http/https may be navigated: file:// would
    read local files straight back to the model, javascript:/data: execute, and chrome://
    exposes browser internals. The scheme was previously passed through unchecked."""
    candidate = (raw or "").strip()
    if not candidate:
        return None, None
    if not candidate.lower().startswith(ALLOWED_SCHEMES):
        return None, f"refused non-http(s) url: {redact(candidate[:60])}"
    return candidate, None


def hide_url_secrets(raw):
    """Drop userinfo and query/fragment values before a URL is persisted or served, since
    query strings routinely carry session tokens and userinfo carries credentials."""
    text = str(raw or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return redact(text)
    if not parts.scheme:
        return redact(text)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "<redacted>" if parts.query else "", ""))


def debug_alive():
    """Confirm the debug port is served by a Chromium browser, not just any local process
    that happens to answer on it."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=3) as response:
            info = json.load(response)
    except Exception:
        return False
    browser = str(info.get("Browser", "")).lower()
    return any(hint in browser for hint in CHROMIUM_HINTS)


def ensure_chrome():
    """Chrome 136+ ignores --remote-debugging-port on the default profile, so a
    dedicated profile is required. Unlike the chrome://inspect toggle, it needs no
    manual click and survives restarts, which is what makes unattended calls work."""
    browser = find_browser()
    if debug_alive():
        return browser, "already-running"
    if not Path(browser).exists():
        raise RuntimeError(f"no Chromium-based browser at {browser}; set JEV_CHROME to override")
    profile = profile_for(browser)
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            browser,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(1)
        if debug_alive():
            return browser, "launched"
    raise RuntimeError(f"{Path(browser).name} debug endpoint on port {DEBUG_PORT} never came up")


TYPESAFE_USD_PER_MTOK_INPUT = 0.042  # published rate; TypeSafe output tokens are free


def _price(name, default):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def price_config():
    return {
        "typesafe_per_mtok_input": _price("JEV_TYPESAFE_PRICE_PER_MTOK_INPUT", TYPESAFE_USD_PER_MTOK_INPUT),
        "text_prompt_per_m": _price("JEV_TEXT_PRICE_PROMPT_PER_M", 0.0),
        "text_completion_per_m": _price("JEV_TEXT_PRICE_COMPLETION_PER_M", 0.0),
    }


def estimate_cost(usage):
    """TypeSafe bills input tokens only (output free) at the published $0.042/MTok.
    The text helper runs on an OpenCode Go subscription, so its marginal rate defaults
    to 0 until JEV_TEXT_PRICE_*_PER_M is set."""
    prices = price_config()
    usd = (
        prices["typesafe_per_mtok_input"] * usage.get("typesafe_input_tokens", 0) / 1_000_000
        + prices["text_prompt_per_m"] * usage.get("prompt_tokens", 0) / 1_000_000
        + prices["text_completion_per_m"] * usage.get("completion_tokens", 0) / 1_000_000
    )
    note = (
        f"TypeSafe input ${prices['typesafe_per_mtok_input']}/MTok (output free); "
        f"text ${prices['text_prompt_per_m']}/MTok prompt, "
        f"${prices['text_completion_per_m']}/MTok completion."
    )
    return round(usd, 8), note


def record_usage(entry):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_ledger():
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def run_search(goal, url=None, max_content_chars=6000, include_log=False):
    """Run one Jev search and return a result the calling LLM can act on."""
    load_env()
    started_iso = datetime.now(timezone.utc).isoformat()
    url_omitted = url is None
    start_url, url_error = safe_url(url)
    if url_error:
        raise ValueError(url_error)
    if start_url is None:
        start_url = DEFAULT_URL + quote_plus(goal)
    max_content_chars = max(200, min(int(max_content_chars), CONTENT_CHAR_CAP))
    browser, chrome_state = ensure_chrome()

    from jev_ultrafast import Agent

    # A run can exhaust the agent's decision budget or fail mid-loop. Capture that and still
    # return and record the outcome: a caller must never get silent empty output, and the cost
    # of a runaway run still belongs in the ledger.
    error = None
    with Agent(start_url, goal) as agent:
        try:
            for _ in agent.run():
                pass
        except Exception as exc:
            error = redact(f"{type(exc).__name__}: {exc}")
        snap = agent.snapshot()

    page = snap["page"]
    history = snap.get("history", [])
    decisions = snap.get("decisions", [])
    text_calls = snap.get("text_calls", [])

    usage = {
        "decisions": len(decisions),
        "typesafe_latency_ms": sum(d.get("latency_ms", 0) or 0 for d in decisions),
        "typesafe_input_tokens": sum((d.get("usage") or {}).get("input_tokens", 0) for d in decisions),
        "typesafe_output_tokens": sum((d.get("usage") or {}).get("output_tokens", 0) for d in decisions),
        "browser_actions": len(history),
        "text_calls": len(text_calls),
        "text_model": text_calls[-1]["model"] if text_calls else None,
        "prompt_tokens": sum((t.get("usage") or {}).get("prompt_tokens", 0) for t in text_calls),
        "completion_tokens": sum((t.get("usage") or {}).get("completion_tokens", 0) for t in text_calls),
        "text_latency_ms": sum(t.get("latency_ms", 0) or 0 for t in text_calls),
    }
    cost, cost_note = estimate_cost(usage)
    usage["est_cost_usd"] = cost
    usage["cost_note"] = cost_note

    content = (page.get("text") or "").strip()
    trimmed = len(content) > max_content_chars
    result = {
        "status": snap.get("status"),
        "error": error,
        "browser": browser,
        "goal": snap.get("goal", goal),
        "start_url": start_url,
        "url_omitted": url_omitted,
        "warning": (
            "url was omitted, so this began as a Google keyword search of the goal text. "
            "Pass url for anything site-specific."
        ) if url_omitted else None,
        "final_url": page.get("url"),
        "title": page.get("title"),
        "content": content[:max_content_chars],
        "content_truncated": trimmed,
        "visited": [
            {"step": h.get("step"), "operation": h.get("operation"), "target": h.get("target"), "text": h.get("text")}
            for h in history
        ],
        "elapsed_ms": snap.get("elapsed_ms"),
        "usage": usage,
        "caveat": (
            "status is the agent's own choice; trust final_url and content as what it actually reached. "
            "BLOCKED usually means no supported control matched, not that the page was empty."
        ),
    }

    record_usage(
        {
            "ts": started_iso,
            "goal": goal,
            "start_url": hide_url_secrets(start_url),
            "final_url": hide_url_secrets(page.get("url")),
            "status": snap.get("status"),
            "error": error,
            "elapsed_ms": snap.get("elapsed_ms"),
            "browser": browser,
            "chrome": chrome_state,
            **usage,
        }
    )

    rows = read_ledger()
    result["totals"] = counter_summary(rows)
    if include_log:
        result["log"] = [
            {
                "ts": row.get("ts"),
                "status": row.get("status"),
                "elapsed_ms": row.get("elapsed_ms"),
                "cost_usd": row.get("est_cost_usd"),
                "goal": row.get("goal"),
            }
            for row in rows[-10:]
        ]
    return result


def aggregate(rows):
    searches = len(rows)
    total_ms = sum(r.get("elapsed_ms") or 0 for r in rows)
    decisions = sum(r.get("decisions") or 0 for r in rows)
    ts_input = sum(r.get("typesafe_input_tokens") or 0 for r in rows)
    prompt = sum(r.get("prompt_tokens") or 0 for r in rows)
    completion = sum(r.get("completion_tokens") or 0 for r in rows)
    costs = [r["est_cost_usd"] for r in rows if r.get("est_cost_usd") is not None]
    _, cost_note = estimate_cost(
        {"typesafe_input_tokens": ts_input, "prompt_tokens": prompt, "completion_tokens": completion}
    )
    totals = {
        "searches": searches,
        "total_elapsed_ms": total_ms,
        "total_elapsed_s": round(total_ms / 1000, 1),
        "avg_elapsed_ms": round(total_ms / searches) if searches else 0,
        "total_decisions": decisions,
        "total_typesafe_input_tokens": ts_input,
        "total_browser_actions": sum(r.get("browser_actions") or 0 for r in rows),
        "total_text_calls": sum(r.get("text_calls") or 0 for r in rows),
        "total_prompt_tokens": prompt,
        "total_completion_tokens": completion,
        "total_tokens": prompt + completion,
        "total_cost_usd": round(sum(costs), 8) if costs else None,
        "cost_note": cost_note,
        "statuses": {s: sum(1 for r in rows if r.get("status") == s) for s in {r.get("status") for r in rows}},
    }
    return totals


def counter_summary(rows):
    """Cumulative time and price counter shown when a search finishes."""
    totals = aggregate(rows)
    return {
        "searches": totals["searches"],
        "total_elapsed_s": totals["total_elapsed_s"],
        "avg_elapsed_ms": totals["avg_elapsed_ms"],
        "total_typesafe_input_tokens": totals["total_typesafe_input_tokens"],
        "total_cost_usd": totals["total_cost_usd"],
    }


def format_stats(totals, recent=None):
    lines = [
        f"Jev usage: {totals['searches']} searches, {totals['total_elapsed_s']}s total "
        f"(avg {totals['avg_elapsed_ms']}ms)",
        f"  decisions={totals['total_decisions']}  browser_actions={totals['total_browser_actions']}  "
        f"text_calls={totals['total_text_calls']}",
        f"  typesafe input tokens={totals['total_typesafe_input_tokens']}",
        f"  text tokens: prompt={totals['total_prompt_tokens']} completion={totals['total_completion_tokens']}",
        "  total cost: "
        + (f"${totals['total_cost_usd']:.6f}" if totals["total_cost_usd"] is not None else "unknown")
        + f"  ({totals['cost_note']})",
        f"  statuses: {totals['statuses']}",
    ]
    if recent:
        lines.append("  recent:")
        for r in recent:
            lines.append(
                f"    {r.get('ts','')[:19]}  {r.get('status'):<8} {r.get('elapsed_ms')}ms  "
                f"{str(r.get('goal'))[:52]}"
            )
    return "\n".join(lines)


DASHBOARD = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Jev browser stats</title>
<style>
:root{color-scheme:dark}
body{margin:0;font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:#0e1116;color:#e6edf3}
header{padding:16px 24px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center}
h1{font-size:14px;margin:0;font-weight:600;letter-spacing:.08em}
.dot{width:8px;height:8px;border-radius:50%;background:#3fb950;display:inline-block;
margin-right:8px;animation:p 1.6s infinite}
@keyframes p{50%{opacity:.25}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;padding:20px 24px}
.card{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:14px}
.k{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:#8b949e}
.v{font-size:21px;font-weight:600;margin-top:6px}
table{width:calc(100% - 48px);margin:0 24px 28px;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d;font-size:12.5px;vertical-align:top}
th{color:#8b949e;font-weight:500;text-transform:uppercase;font-size:10.5px;letter-spacing:.07em}
tr:hover td{background:#161b22}
.done{color:#3fb950}.blocked{color:#d29922}.error{color:#f85149}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}
.muted{color:#8b949e}
</style></head><body>
<header><h1>JEV BROWSER STATS</h1>
<span><span class="dot"></span><span class="muted" id="ts">connecting</span></span></header>
<div class="grid">
<div class="card"><div class="k">Searches</div><div class="v" id="s">-</div></div>
<div class="card"><div class="k">Total time</div><div class="v" id="t">-</div></div>
<div class="card"><div class="k">Avg / search</div><div class="v" id="a">-</div></div>
<div class="card"><div class="k">Est. cost</div><div class="v" id="c">-</div></div>
<div class="card"><div class="k">TypeSafe tokens</div><div class="v" id="tk">-</div></div>
<div class="card"><div class="k">Decisions</div><div class="v" id="d">-</div></div>
</div>
<table><thead><tr><th>Time</th><th>Status</th><th>Took</th><th>Cost</th>
<th>Goal</th><th>Result</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const n=(v,d=0)=>v==null?"-":Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
async function tick(){
 try{
  const d=await (await fetch("/api/stats",{cache:"no-store"})).json(); const T=d.totals;
  s.textContent=n(T.searches); t.textContent=n(T.total_elapsed_s,1)+"s";
  a.textContent=n((T.avg_elapsed_ms||0)/1000,1)+"s";
  c.textContent=T.total_cost_usd==null?"?":"$"+n(T.total_cost_usd,5);
  tk.textContent=n(T.total_typesafe_input_tokens); d_el.textContent=n(T.total_decisions);
  ts.textContent="live "+new Date().toLocaleTimeString();
  rows.replaceChildren(...d.recent.map(x=>{
   const tr=document.createElement("tr");
   const cell=(text,cls)=>{const td=document.createElement("td"); if(cls)td.className=cls;
     td.textContent=text; tr.appendChild(td);};
   cell((x.ts||"").slice(11,19),"muted");
   cell(x.status||"",x.status||"");
   cell(n((x.elapsed_ms||0)/1000,1)+"s");
   cell(x.est_cost_usd==null?"?":"$"+n(x.est_cost_usd,5));
   cell((x.goal||"").slice(0,58));
   const td=document.createElement("td"), a=document.createElement("a");
   const u=String(x.final_url||"");
   if(/^https?:\/\//i.test(u)){a.href=u; a.target="_blank"; a.rel="noreferrer";}
   a.textContent=u.replace(/^https?:\/\//,"").slice(0,46);
   td.appendChild(a); tr.appendChild(td);
   return tr;
  }));
 }catch(e){ ts.textContent="dashboard up, ledger unreachable"; }
}
const d_el=document.getElementById("d");
tick(); setInterval(tick,2000);
</script></body></html>
"""


def build_server(port=8767):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def send_text(self, code, body, mime="application/json"):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            # Reject non-loopback Host headers so a DNS-rebinding page cannot read the ledger.
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                self.send_text(403, json.dumps({"error": "forbidden"}))
                return
            if self.path.startswith("/api/stats"):
                rows = read_ledger()
                self.send_text(200, json.dumps({"totals": aggregate(rows), "recent": rows[-30:][::-1]}))
            else:
                self.send_text(200, DASHBOARD, "text/html")

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port=8767):
    build_server(port).serve_forever()


try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    MCPServer = None


if MCPServer is not None:
    server = MCPServer(
        name="jev",
        version="0.1.0",
        instructions=(
            "Jev is a real Chrome browser agent. Use jev_search whenever you need live web "
            "information, a page fetched or read, a form filled, or a result found that you "
            "cannot get from your own knowledge. Give it a URL and one natural-language goal; "
            "it picks the clicks and typing itself and returns the final URL plus the visible "
            "page text for you to work with."
        ),
    )

    @server.tool(
        description=(
            "Live web lookup: drives a real Chrome browser to a URL and returns the page's visible "
            "text. Use ONLY for current, real-world info you cannot know from training (prices, "
            "availability, live pages) - not for general knowledge, math, or code. ALWAYS pass url: "
            "the exact page to open (e.g. 'https://www.google.com/travel/flights?hl=en'). Omitting "
            "url runs a Google keyword search of your goal sentence - useless for sentence-like "
            "goals. Takes 15-60s; wait, do not retry. content is page text to reason over; final_url "
            "is ground truth. status:'done' is the agent's claim, not proof of success. Every "
            "result ends with a cumulative totals counter (searches, total time, total cost); "
            "pass include_log=true to also get the recent search log."
        )
    )
    def jev_search(
        goal: str, url: str = "", max_content_chars: int = 6000, include_log: bool = False
    ) -> str:
        try:
            result = run_search(goal, url or None, max_content_chars, include_log)
        except Exception as error:
            result = {"status": "error", "goal": goal, "error": redact(f"{type(error).__name__}: {error}")}
        return json.dumps(result, indent=2, ensure_ascii=False)

    @server.tool(
        description=(
            "Usage/cost telemetry for the Jev browser agent. Call ONLY after a jev_search run, when "
            "you need to report or verify how many searches ran, how long they took, or their "
            "estimated cost. Do NOT call to perform a search or to answer the user's question - it "
            "returns no page content. Returns per-run totals: search count, total/average elapsed "
            "time, model decision count, browser actions, text-helper token totals, and estimated "
            "cost (only when price env vars are configured). Takes under a second."
        )
    )
    def jev_stats(limit: int = 10) -> str:
        rows = read_ledger()
        totals = aggregate(rows)
        recent = rows[-limit:] if limit and limit > 0 else []
        return json.dumps({"totals": totals, "recent": recent, "summary": format_stats(totals, recent)},
                          indent=2, ensure_ascii=False)


KEYS = (
    ("TYPESAFE_API_KEY", "TypeSafe key - drives every decision (required)"),
    ("TEXT_MODEL_API_KEY", "text-model key - required for typing, e.g. an OpenCode Go key"),
    ("TEXT_MODEL_BASE_URL", "OpenAI-compatible endpoint, e.g. https://opencode.ai/zen/go/v1"),
    ("TEXT_MODEL", "model id, e.g. deepseek-v4-flash"),
    ("JEV_CHROME", "browser path - blank auto-detects a Chromium fork"),
)


def configure():
    load_env()
    path = ROOT / ".env"
    if not path.exists():
        template = ROOT / ".env.example"
        path.write_text(template.read_text(encoding="utf-8") if template.exists() else "", encoding="utf-8")
        print(f"created {path}")
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "=" in line]

    print("Detected Chromium-based browsers:")
    for candidate in BROWSER_FORKS:
        full = os.path.expandvars(candidate)
        if Path(full).exists():
            print(f"  [x] {full}")
    print(f"selected: {find_browser()}\n")

    for name, hint in KEYS:
        current = next((line.partition("=")[2].strip() for line in lines if line.startswith(name + "=")), "")
        print(f"{name}  ({hint})  [{'set' if current else 'empty'}]")
        value = input("  new value, or Enter to keep: ").strip()
        if value:
            lines = [line for line in lines if not line.startswith(name + "=")] + [f"{name}={value}"]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    gitignore = ROOT / ".gitignore"
    ignored = gitignore.exists() and ".env" in gitignore.read_text(encoding="utf-8")
    print(f"\nwrote {path}   git-ignored: {ignored}")
    print(f"browser in use: {find_browser()}")
    print("Restart your LLM harness so the MCP server reloads.")


def main(argv):
    # Windows Python defaults stdout to cp1252, so printing page text containing accented or
    # CJK characters raised UnicodeEncodeError and emitted no output at all.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--wipe-sandbox" in argv:
        profiles = sandbox_profiles()
        for path in profiles:
            try:
                shutil.rmtree(path)
                print(f"removed {path}")
            except OSError as exc:
                print(f"could not remove {path}: {exc}", file=sys.stderr)
        print(f"{len(profiles)} sandbox profile(s) handled")
        return 0
    if "--configure" in argv:
        configure()
        return 0
    if "--browsers" in argv:
        for candidate in BROWSER_FORKS:
            full = os.path.expandvars(candidate)
            print(f"{'[x]' if Path(full).exists() else '[ ]'} {full}")
        browser = find_browser()
        print(f"selected: {browser}")
        print(f"profile : {profile_for(browser)}")
        if os.environ.get("JEV_SANDBOX") == "1":
            print("sandbox : ON (JEV_SANDBOX=1)")
        return 0
    if "--serve" in argv:
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8767
        serve(port)
        return 0
    if "--stats" in argv:
        rows = read_ledger()
        print(format_stats(aggregate(rows), rows[-10:]))
        return 0
    if "--search" in argv:
        idx = argv.index("--search")
        goal = argv[idx + 1] if len(argv) > idx + 1 else None
        if not goal:
            print("usage: --search \"<goal>\" [--url <url>] [--log]")
            return 2
        url = argv[argv.index("--url") + 1] if "--url" in argv else None
        try:
            result = run_search(goal, url, include_log="--log" in argv)
        except Exception as exc:
            result = {"status": "error", "goal": goal, "url": url,
                      "error": redact(f"{type(exc).__name__}: {exc}")}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        totals = result.get("totals")
        if totals:
            print(
                f"[jev] {totals['searches']} searches  {totals['total_elapsed_s']}s total  "
                f"${totals['total_cost_usd']} cumulative",
                file=sys.stderr,
            )
        return 0 if result.get("status") != "error" else 1
    if MCPServer is None:
        print("mcp package missing; run: uv add mcp", file=sys.stderr)
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
