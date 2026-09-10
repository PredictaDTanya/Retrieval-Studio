# Security policy

## Reporting a vulnerability
Please report suspected vulnerabilities privately via GitHub's
**Security ▸ Report a vulnerability** (private advisory) on this repository —
not in a public issue. Include reproduction steps and affected versions.
You should receive an acknowledgement within 7 days.

## Scope notes for users

- **API keys.** The app reads `OPENAI_API_KEY` from the environment or a
  masked, session-only input field. Keys are never written to disk, logged,
  exported, or accepted via URL parameters. Rotate any key you suspect has
  been exposed (platform.openai.com/api-keys).
- **Raw responses are sensitive.** `data/studies/**/raw/*.json` contains your
  full prompts and the model's complete answers — which may include client
  names, research questions and commercial strategy. The `data/` tree is
  gitignored; never commit it, and treat exports the same way.
- **Spend safety.** Runs are capped at 500 API calls, show a cost estimate,
  and require explicit confirmation above ~$2. Fund the OpenAI account with
  prepaid credit and keep auto-recharge off for a hard ceiling.
- **Lead data is personal data.** In gated hosted mode the access gate
  collects name + email into `data/leads.jsonl` (and an optional webhook).
  Treat that file and the webhook destination as PII: keep them secure, and
  don't commit `data/`. API keys are never stored in a lead record.
- **Deploy modes.** `local` (default) is single-user on localhost. `hosted`
  adds per-session isolation and an optional access-code gate — safe for a
  **gated beta** (share the URL + code with a controlled audience). A fully
  public, anonymous deployment additionally needs rate-limiting, request
  timeouts and legal pages (see docs/HOSTING.md "Not yet built"); don't open
  it to the public internet until those exist.
