# Jev Browser Kit

Give any MCP-capable LLM harness (opencode, Codex, Claude Code, DeepSeek Harness, Cursor)
the ability to drive a **real Chrome browser** from one natural-language goal and read the
result back.

TypeSafe's **Jev** model picks the operation (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_*`,
`WAIT`, `DONE`, `BLOCKED`) plus an indexed element; a small text model generates values only
for `TYPE_TEXT`. Code owns the workflow - the model never emits selectors or JavaScript.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Options: `-RepoPath <dir>` (default `~\Documents\browseruse`), `-NoRegister`, `-SkipVerify`.

The installer is **idempotent** - re-running it is safe. It never overwrites an existing
`.env` and never duplicates an MCP registration.

What it does:

1. Ensures Python + `uv`
2. Clones `browser-use/jev-ultrafast`
3. Applies two required patches (see below)
4. `uv sync` + installs the MCP SDK, drops in `jev_mcp.py`
5. Creates `.env` from the template if absent
6. Registers the MCP server in opencode and Codex when their configs exist
7. Runs the project's own `pytest` and `ruff`

## Configure

Guided setup - writes `.env` for you and never echoes the values back:

```powershell
python jev_mcp.py --configure
```

Or edit `<repo>\.env` by hand:

```
TYPESAFE_API_KEY=...        # required - drives every decision
TEXT_MODEL_API_KEY=...      # required - or TYPE_TEXT cannot run and no field can be filled
TEXT_MODEL_BASE_URL=...     # any OpenAI-compatible endpoint
TEXT_MODEL=...              # e.g. deepseek-v4-flash
TEXT_MODEL_REASONING=none
```

Then **restart your harness** so it loads the MCP server.

## Use

In your harness, just ask - the model calls the tools itself:

> "Use jev to find the current price of the Nike Air Max 90 on Google Images."
> "How much have my jev searches cost?"

Tools exposed:

| Tool | Returns |
|---|---|
| `jev_search(goal, url?, max_content_chars?, include_log?)` | `status`, `final_url`, `title`, `content` (visible page text), `visited` action trace, `elapsed_ms`, `usage`, and a cumulative **`totals` counter** |
| `jev_stats(limit?)` | searches, total/avg elapsed, TypeSafe input tokens, decisions, text tokens, estimated cost |

### Time and price counter

Every `jev_search` result ends with a cumulative counter, so the caller always knows the
running cost without a second call:

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
cost, goal). The CLI prints a one-line counter to stderr after every search:

```
[jev] 8 searches  172.2s total  $0.01861104 cumulative
```

From the terminal:

```powershell
# one-off search (add --log for the search log)
python jev_mcp.py --search "<goal>" --url "https://www.google.com/travel/flights?hl=en" --log

# usage + cost totals
python jev_mcp.py --stats

# live stats dashboard
python jev_mcp.py --serve        # http://127.0.0.1:8767
```

**Always pass `url`.** It is the starting page. Omitting it runs a Google keyword search of
the raw goal text, which is wrong for sentence-like goals.

## Cost

TypeSafe bills **input tokens only** at the published `$0.042/MTok`; output tokens are free.
Override with `JEV_TYPESAFE_PRICE_PER_MTOK_INPUT`. The text helper is assumed to be a
subscription (marginal $0) unless `JEV_TEXT_PRICE_PROMPT_PER_M` / `JEV_TEXT_PRICE_COMPLETION_PER_M`
are set. A typical search lands around **$0.001**.

Every search is appended to `<repo>\artifacts\jev_usage.jsonl`.

## Why the two patches

**1. `browser.py` - interaction settle (3000ms).** Google Flights fades dropdowns in over
~2-3s. Upstream finishes its post-interaction settle after 2 animation frames (~33ms) or a
50/200ms cap, so the snapshot catches the menu at `opacity: 0`, `checkVisibility()` returns
false, the menu options are dropped, and the agent clicks the same control until it blocks.
Cost of the fix: a full search takes ~40s instead of the README's ~7s. Do not lower it.

**2. `model.py` - client session header.** OpenCode Go's client spec requires a stable
`x-opencode-session` and a real user agent; without them every text call returns
`400 MissingSessionID`. The header is sent only for `opencode.ai` endpoints.

## Browser choice - read before swapping engines

Jev clicks by **geometry**: `getBoundingClientRect()`, `elementFromPoint()` occlusion checks,
`checkVisibility()`, then `Input.dispatchMouseEvent` at x/y. That rules out anything without
a real layout engine.

| Browser | Weight | Works with jev? |
|---|---|---|
| **Chromium / Chrome / Edge** | ~200-400MB focused | **Yes** - the only practical option |
| Playwright `chromium_headless_shell` | lighter than full Chrome | Yes (full Blink layout) |
| Lightpanda (Zig) | ~21MB peak, ~19x lighter, ~9x faster | **No** - explicitly *no rendering engine*, so there is no geometry to click |
| Carbonyl | fast, ~0% idle CPU | No - Linux/macOS only, renders to terminal |
| Browsh | ~50x CPU of Carbonyl | No - Firefox headless + screenshot pipeline |
| Servo / Ladybird | real engines, incomplete | No - no supported automation path |

Chrome requires a **dedicated profile** (`--user-data-dir`) for remote debugging, because
Chrome 136+ ignores `--remote-debugging-port` on the default profile. The inspect toggle in
`chrome://inspect` is per browser instance and needs re-clicking after every restart, so it
cannot be used for unattended runs. `jev_mcp.py` launches and reuses the dedicated profile
automatically.

### Using your own Chromium fork (Thorium, Brave, Edge, ...)

Any Chromium-based browser works. Auto-detection covers Chrome, Thorium, Chromium, Brave,
Edge, Vivaldi and Opera. List what is found:

```powershell
python jev_mcp.py --browsers
```

Choose one interactively with `python jev_mcp.py --configure`, or set it explicitly:

```
JEV_CHROME=C:\Users\you\AppData\Local\Thorium\Application\thorium.exe
```

Each browser gets its **own profile directory** (`~/.cache/jev-<browser>-profile`). That is
required, not cosmetic: a Chromium fork *older* than the one that created a profile refuses
to open it, so sharing one directory would break the working setup on switch. Chrome keeps
`~/.cache/jev-chrome-profile`, so an already-consented profile survives. A brand-new profile
needs Google's consent interstitial cleared once.

Firefox is **not** supported - jev needs Blink plus CDP.

## Known limits

- Google's consent interstitial is shadow-DOM and yields 0 actions (the run ends `BLOCKED`).
  Clear it once in the debug profile and the cookie persists.
- Menus slower than ~3s to appear will still be missed.
- Out of scope upstream: shadow roots, iframes, canvas, uploads, pop-up tabs, nested
  scrolling, arbitrary keyboard widgets.
- A `DONE` status is the model's own choice - trust `final_url` and `content` as what it
  actually reached.
- Runs are not fully deterministic. The same Google Images goal succeeded on one attempt
  and exhausted the agent's 120-decision budget on another. Failures now return an error
  object and are still recorded in the ledger, instead of emitting nothing.
- The CLI forces stdout to UTF-8. Windows Python defaults to cp1252, which crashed on
  accented page text and produced empty output. Do not remove that reconfigure.
- `url` defaults to a Google keyword search of the goal text. Always pass an explicit
  `url` for anything site-specific.
