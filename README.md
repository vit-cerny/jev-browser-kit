# Jev Browser Kit

Give any MCP-capable LLM harness (opencode, Codex, Claude Code, DeepSeek Harness) an
on-demand **real-browser search tool**. One natural-language goal in, the page's contents
out, with per-run timing and cost tracking.

It wraps [TypeSafe Jev](https://docs.typesafe.ai/introduction) and the upstream
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) agent in an MCP
server you can call from your model.

- **One goal, no selectors.** The model picks an operation (`CLICK`, `TYPE_TEXT`,
  `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, `BLOCKED`) and an indexed element.
  It never emits CSS, coordinates, or executable JavaScript - the code owns the workflow.
- **A real Blink browser.** It drives Chrome (or any Chromium fork) over CDP, so it can
  click, type, and read pages that a plain HTTP fetch cannot.
- **Full output kept.** Long pages are returned **untruncated** as `full_content` and also
  written to disk, so nothing is lost to the caller's context cap.
- **Cost-aware.** TypeSafe bills input tokens only; every result carries a cumulative
  time/price counter, so the caller always knows the running cost.

## Quick start

Three commands, then restart your harness:

```powershell
git clone https://github.com/vit-cerny/jev-browser-kit
powershell -ExecutionPolicy Bypass -File install.ps1
python jev_mcp.py --configure
```

`install.ps1` does the whole setup in one shot: checks Python + uv, clones
`browser-use/jev-ultrafast`, applies the two required patches, installs dependencies,
drops in `jev_mcp.py` and `scripts/jev-search.ps1`, creates `.env` from the template, and
registers the MCP server in opencode and Codex when their configs exist. It is
idempotent: re-running is safe, it never overwrites an existing `.env`, and it never
duplicates a registration.

Options: `-RepoPath <dir>` (default `%USERPROFILE%\Documents\browseruse`), `-NoRegister`,
`-SkipVerify`.

`python jev_mcp.py --configure` is the interactive wizard. Run it from the directory where
`jev_mcp.py` landed (the default is `%USERPROFILE%\Documents\browseruse`). It writes your
`.env`, never echoes values back, and lets you pick a browser.

Then restart your harness so it loads the MCP server. That is it: ask for a live lookup
and the model calls `jev_search` itself.

## What you get back

Every `jev_search` returns a JSON object. The fields that matter most:

| Field | Meaning |
|---|---|
| `status` | The agent's own choice (`done`, `blocked`, `predicted`, `error`). A claim, not proof. |
| `final_url` | The page the browser actually ended on. Treat this as ground truth. |
| `title` | The final page title. |
| `content` | Visible page text, capped at `max_content_chars` (default 6000, range 200-100000). |
| `content_truncated` | `true` when `content` was cut. |
| `content_chars` | Length of the full page text, in characters. |
| `full_content` | **The complete, untruncated page text** - kept so the model can analyze the whole page at the end. |
| `output_file` | Path to the saved JSON copy of this whole result under `artifacts/jev_outputs/`. |
| `visited` | The action trace: step, operation, target, text. |
| `elapsed_ms`, `usage`, `totals` | Timing, per-run tokens/cost, and the cumulative counter. |

### Full output is saved, printed, and returned

Nothing is dropped. For every search:

1. **Saved** - the entire result (including `full_content` and the action trace) is written
   to `artifacts/jev_outputs/<timestamp>-<goal>.json`. The path comes back as `output_file`.
2. **Returned** - `full_content` carries the untruncated page text, so your model can
   analyze all of it at the end instead of only the `content` slice.
3. **Printed** - the CLI (`--search`) prints the JSON and then the full page text, followed
   by the saved path on stderr.

`content` stays capped by design: it is the compact slice for the running loop, while
`full_content` is the complete record. Use `content` while reasoning, `full_content` or the
saved file when you need every line.

## Use it from your LLM

Just ask. The model calls the tools itself:

> "Use jev to find the current price of the Nike Air Max 90 on Google Images."
> "How much have my jev searches cost?"

| Tool | Returns |
|---|---|
| `jev_search(goal, url?, max_content_chars?, include_log?)` | `status`, `final_url`, `title`, `content`, `full_content`, `output_file`, the `visited` action trace, `elapsed_ms`, `usage`, and a cumulative `totals` counter |
| `jev_stats(limit?)` | searches, total/avg elapsed, TypeSafe input tokens, decisions, text tokens, estimated cost |

**Always pass `url`.** It is the starting page. Omitting it runs a Google keyword search of
the raw goal text, which is wrong for sentence-like goals.

Every `jev_search` result ends with a cumulative time/price counter, so the caller knows
the running cost without a second call:

```json
"totals": {
  "searches": 8,
  "total_elapsed_s": 172.2,
  "avg_elapsed_ms": 21524,
  "total_typesafe_input_tokens": 443120,
  "total_cost_usd": 0.01861104
}
```

Pass `include_log: true` to also get the last 10 searches (timestamp, status, duration,
cost, goal). The CLI prints a one-line counter to stderr after every search.

## Add your API keys

Run `python jev_mcp.py --configure` and paste the values when asked. It writes
`<repo>\.env` (owner-only) and never prints the values back.

Two keys are required:

- `TYPESAFE_API_KEY` - drives every Jev decision.
- `TEXT_MODEL_API_KEY` - powers `TYPE_TEXT` (typing into fields). Any OpenAI-compatible
  endpoint works; the defaults point at OpenCode Go.

You can also edit `<repo>\.env` by hand. The template lives at `.env.example`, which also
documents the optional variables (`JEV_CHROME`, `JEV_SANDBOX`, price overrides, and more).

## Pick your browser

Any Chromium-based browser works: Chrome, Thorium, Chromium, Brave, Edge, Vivaldi, Opera.
`jev_mcp.py` auto-detects them. List what it finds:

```powershell
python jev_mcp.py --browsers
```

Pick one in `--configure`, or override explicitly with `JEV_CHROME` in `.env`:

```
JEV_CHROME=%LOCALAPPDATA%\Thorium\Application\thorium.exe
```

Firefox does NOT work. Jev clicks by geometry (`getBoundingClientRect`,
`elementFromPoint`, `checkVisibility`) over CDP, so it needs a real Blink layout engine.

Each browser gets its own profile directory at `~/.cache/jev-<browser>-profile`. That is
required, not cosmetic: a Chromium fork older than the one that created a profile refuses
to open it, so sharing one directory would break the setup on switch. Chrome keeps
`~/.cache/jev-chrome-profile`, so an already-consented profile survives. A brand-new
profile needs Google's consent interstitial cleared once. Set `JEV_SANDBOX=1` for an
isolated profile that never sees your normal browsing session; reset it with
`python jev_mcp.py --wipe-sandbox`.

## What it costs

TypeSafe bills input tokens only at the published `$0.042/MTok`; output tokens are free.
The text helper defaults to a subscription, so its marginal rate is 0. A typical search
lands around **$0.001**.

Every search is appended to `<repo>\artifacts\jev_usage.jsonl` (metadata only, with URLs
stripped of secrets). Full page text goes to `<repo>\artifacts\jev_outputs\`.

## From the terminal

```powershell
python jev_mcp.py --search "<goal>" --url "https://www.google.com/travel/flights?hl=en" --log
python jev_mcp.py --stats
python jev_mcp.py --serve        # live dashboard at http://127.0.0.1:8767
```

`scripts/jev-search.ps1` is a one-shot wrapper that starts Chrome, runs one goal, and
prints the result:

```powershell
.\scripts\jev-search.ps1 -Goal "Find the cheapest flight from Prague to Barcelona"
```

## Files in this repo

| File | Job |
|---|---|
| `jev_mcp.py` | The MCP server + CLI: search, stats dashboard, setup wizard, output saving |
| `install.ps1` | One-shot installer (clone, patch, install, register) |
| `scripts/jev-search.ps1` | One-shot search wrapper for the terminal |
| `scripts/scan-secrets.ps1` | Dependency-free secret scanner |
| `.env.example` | Template for keys and optional overrides |
| `tests/` | Offline tests (no paid API calls) |
| `SECURITY.md` | Threat model, scope, and credential policy |

## Limits

- Google's consent interstitial is shadow-DOM and yields 0 actions (the run ends
  `BLOCKED`). Clear it once in the debug profile and the cookie persists.
- Menus slower than ~3s to appear will still be missed.
- Out of scope upstream: shadow roots, iframes, canvas, uploads, pop-up tabs, nested
  scrolling, arbitrary keyboard widgets.
- A `DONE` status is the model's own choice, not proof. Trust `final_url` and the saved
  content as what it actually reached.
- Runs are not fully deterministic. The same goal can succeed on one attempt and exhaust
  the agent's 120-decision budget on another. Failures return an error object and are still
  recorded in the ledger.
- A search takes 15-60s. Wait, do not retry.

## Security

- No API keys are ever committed. `.env` is git-ignored (and written owner-only), and
  `--configure` never echoes values back. CI fails if `.env` is ever tracked by git, and
  gitleaks scans every push.
- **Non-http(s) start URLs are refused.** `url` is supplied by your LLM, so `file://` would
  otherwise read local files - including `.env` - straight back into the model's context.
  `javascript:`, `data:` and `chrome://` are refused too.
- Credentials are redacted before they can reach a log, a tool result or the dashboard:
  `apikey_`/`sk-`/`ghp_`/`github_pat_`/`AKIA` shapes are masked in error text, and userinfo
  plus query values are stripped from URLs before they are persisted, since query strings
  routinely carry session tokens.
- The stats dashboard binds to `127.0.0.1` only **and** rejects non-loopback `Host`
  headers, so a DNS-rebinding page cannot read your search ledger.
- Run `scripts/scan-secrets.ps1` any time; it is dependency-free and masks anything it
  prints. Pass `-FailOnFind` to use it as a pre-commit gate.
- The project deliberately avoids piping remote scripts into a shell: there is no
  `irm | iex` one-liner. Run `install.ps1` from a clone you trust.
- The CDP debug port itself is unauthenticated by design (a Chrome limitation) - treat any
  process that can reach it as able to control the browser.
