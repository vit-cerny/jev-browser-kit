# Jev Browser Kit

Give any MCP-capable LLM harness (opencode, Codex, Claude Code, DeepSeek Harness) a real Chrome browser it can drive from one natural-language goal, and read the result back.

## Quick start

Three commands, then restart your harness:

```powershell
git clone <this repo>
powershell -ExecutionPolicy Bypass -File install.ps1
python jev_mcp.py --configure
```

`install.ps1` does the whole setup in one shot: checks Python + uv, clones `browser-use/jev-ultrafast`, applies two required patches, installs dependencies, drops in `jev_mcp.py`, creates `.env` from the template, and registers the MCP server in opencode and Codex when their configs exist. It is idempotent: re-running is safe, it never overwrites an existing `.env`, and it never duplicates a registration.

Options: `-RepoPath <dir>` (default `%USERPROFILE%\Documents\browseruse`), `-NoRegister`, `-SkipVerify`.

`python jev_mcp.py --configure` is the interactive wizard. Run it from the directory where `jev_mcp.py` landed (the default is `%USERPROFILE%\Documents\browseruse`). It writes your `.env`, never echoes values back, and lets you pick a browser.

Then restart your harness so it loads the MCP server. You're done: ask for a live lookup and the model calls `jev_search` itself.

## Add your API keys

Run `python jev_mcp.py --configure` and paste the values when asked. It writes `<repo>\.env` and never prints the values back.

Two keys are required:

- `TYPESAFE_API_KEY` - drives every Jev decision.
- `TEXT_MODEL_API_KEY` - powers `TYPE_TEXT` (typing into fields). Any OpenAI-compatible endpoint works; the defaults point at OpenCode Go.

You can also edit `<repo>\.env` by hand. The template lives at `.env.example`.

## Pick your browser

Any Chromium-based browser works: Chrome, Thorium, Chromium, Brave, Edge, Vivaldi, Opera. `jev_mcp.py` auto-detects them. List what it finds:

```powershell
python jev_mcp.py --browsers
```

Pick one in `--configure`, or override explicitly with `JEV_CHROME` in `.env`:

```
JEV_CHROME=%LOCALAPPDATA%\Thorium\Application\thorium.exe
```

Firefox does NOT work. Jev clicks by geometry (`getBoundingClientRect`, `elementFromPoint`, `checkVisibility`) over CDP, so it needs a real Blink layout engine.

Each browser gets its own profile directory at `~/.cache/jev-<browser>-profile`. That's required, not cosmetic: a Chromium fork older than the one that created a profile refuses to open it, so sharing one directory would break the setup on switch. Chrome keeps `~/.cache/jev-chrome-profile`, so an already-consented profile survives. A brand-new profile needs Google's consent interstitial cleared once.

## Use it from your LLM

Just ask. The model calls the tools itself:

> "Use jev to find the current price of the Nike Air Max 90 on Google Images."
> "How much have my jev searches cost?"

| Tool | Returns |
|---|---|
| `jev_search(goal, url?, max_content_chars?, include_log?)` | `status`, `final_url`, `title`, `content` (visible page text), `visited` action trace, `elapsed_ms`, `usage`, and a cumulative `totals` counter |
| `jev_stats(limit?)` | searches, total/avg elapsed, TypeSafe input tokens, decisions, text tokens, estimated cost |

**Always pass `url`.** It is the starting page. Omitting it runs a Google keyword search of the raw goal text, which is wrong for sentence-like goals.

Every `jev_search` result ends with a cumulative time/price counter, so the caller always knows the running cost without a second call:

```json
"totals": {
  "searches": 8,
  "total_elapsed_s": 172.2,
  "avg_elapsed_ms": 21524,
  "total_typesafe_input_tokens": 443120,
  "total_cost_usd": 0.01861104
}
```

Pass `include_log: true` to also get the last 10 searches (timestamp, status, duration, cost, goal). The CLI prints a one-line counter to stderr after every search.

From the terminal:

```powershell
python jev_mcp.py --search "<goal>" --url "https://www.google.com/travel/flights?hl=en" --log
python jev_mcp.py --stats
python jev_mcp.py --serve        # live dashboard at http://127.0.0.1:8767
```

## What it costs

TypeSafe bills input tokens only at the published `$0.042/MTok`; output tokens are free. The text helper defaults to a subscription, so its marginal rate is 0. A typical search lands around **$0.001**.

Every search is appended to `<repo>\artifacts\jev_usage.jsonl`.

## Limits

- Google's consent interstitial is shadow-DOM and yields 0 actions (the run ends `BLOCKED`). Clear it once in the debug profile and the cookie persists.
- Menus slower than ~3s to appear will still be missed.
- Out of scope upstream: shadow roots, iframes, canvas, uploads, pop-up tabs, nested scrolling, arbitrary keyboard widgets.
- A `DONE` status is the model's own choice, not proof. Trust `final_url` and `content` as what it actually reached.
- Runs are not fully deterministic. The same goal can succeed on one attempt and exhaust the agent's 120-decision budget on another. Failures return an error object and are still recorded in the ledger.
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