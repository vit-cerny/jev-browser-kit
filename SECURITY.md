# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately:

1. Use GitHub's **private vulnerability reporting** on this repository
   (Repository -> Security -> Report a vulnerability), or
2. Open a private issue via the Security tab if private reporting is enabled.

Include, when possible:

- A description of the vulnerability and its impact.
- Steps to reproduce (minimal, if feasible).
- Affected version / commit.
- Any suggested fix.

You will get an acknowledgment within a few days. We do not run a bug bounty
program; reports are handled on a best-effort basis.

## Threat model

Jev Browser Kit is a **local developer tool**. It is not a network service:

- The MCP server runs on your own machine and binds to `127.0.0.1` only - it is
  not exposed to the network.
- API keys (`TYPESAFE_API_KEY`, `TEXT_MODEL_API_KEY`) live in a **git-ignored
  `.env`** file in the repo directory. They are read only by the local process
  and are never sent anywhere except the provider endpoints you configure.
- The MCP server is driven by **your own LLM harness**. The model chooses
  browser operations (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_*`, `WAIT`,
  `DONE`, `BLOCKED`) against indexed elements; the code owns the workflow and
  the model never emits selectors or executable JavaScript.
- The tool drives a real Chrome browser on your machine, so it can navigate to
  arbitrary URLs you (or your harness) ask it to visit.

## In scope

- Committed secrets: any real API key, token, or credential accidentally
  committed to this repository.
- Code execution or data exfiltration via crafted input to `jev_mcp.py`
  (CLI arguments, `.env` parsing, MCP tool arguments).
- The MCP server binding to anything other than loopback, or otherwise
  exposing local data to the network.
- Unsafe handling of the browser profile, artifacts, or usage ledger.

## Out of scope

- **Prompt injection / malicious websites**: the browser can be pointed at any
  site, and page content is fed back to the model. A hostile page influencing
  the model's choices is expected behavior of an LLM-driven browser tool, not a
  vulnerability in this kit. Do not run it against untrusted content with
  sensitive consequences.
- The upstream `browser-use/jev-ultrafast` project and its dependencies -
  report those to their own maintainers.
- Websites blocking or rate-limiting automated browsing.
- Phishing or abuse performed *through* the tool by the user's own harness.
- General LLM misbehavior (hallucinated `DONE`, wrong clicks, etc.).

## Credentials policy

**No credentials are ever committed to this repository.**

- `.env` and `.env.*` are git-ignored (only `.env.example` is tracked, and it
  contains empty placeholders only).
- CI runs a gitleaks scan on every push and pull request and fails if any
  secret pattern is found.
- CI also fails if `.env` is ever tracked by git.
- If you believe a real key was committed, rotate it immediately and report it
  via the channels above.