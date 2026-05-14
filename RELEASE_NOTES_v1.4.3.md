# v1.4.3 — UX polish (JELabs pilot follow-up #3)

Three usability fixes for the pilot experience. Each one was directly observed during the JELabs live test (2026-05-13): the counterparty saw less polished output than the persona file claimed, and the owner saw L4 alerts for content they themselves declared public.

## What's in

### 1. Lark/Feishu markdown sanitisation (`paid/hermes_io.py`)

`send_dm()` now strips markdown formatting on outbound when the target platform is `feishu` or `lark`. Lark Suite's `text` msg_type doesn't render markdown — bold, bullets, headings, and link syntax all show as literal characters. The LLM emits markdown freely (it's a chat-AI default), so without a strip layer counterparties see `**JE Labs**` with the asterisks.

Transforms applied:

| In | Out |
|---|---|
| `**bold**`, `__bold__` | `bold` |
| `*italic*`, `_italic_` | `italic` |
| `~~strike~~` | `strike` |
| `` `code` `` | `code` |
| `# Heading` | `Heading` |
| `[text](url)` | `text (url)` |
| `- bullet` | `• bullet` (Unicode bullet) |

Telegram and Slack render markdown natively — they pass through unchanged.

### 2. Hard-rule length cap (`paid/decision.py::_DIRECT_HARD_RULES_*`)

Added a new rule (#4) to both Chinese and English hard-rule blocks, instructing the LLM that direct-state replies must be:

- Simple factual question (address / timezone / email): 1 sentence
- General question: 1-3 sentences
- Complex scheduling negotiation: max 4-5 sentences
- No markdown
- No bullet lists unless the sender explicitly asks "list X"

Pre-v1.4.3 the persona file's "1-3 sentences" hint was advisory — and the LLM treated it as a suggestion. The hard-rule block has higher priority and uses imperative wording, which empirically the model respects.

### 3. L4 observer public-material whitelist (`paid/safety.py`)

`detect_cross_cp_name_leakage` and `detect_pii` now accept an optional `whitelist` parameter. `check_output()` auto-loads it from `~/.hermes/paid/sop.md`'s section titled `## 公开材料`, `## Public materials`, `## Whitelist`, `## Whitelist for L4`, or `## Public info` (case-insensitive, any depth `##`+).

Detected entities (cross-cp names, emails, etc.) that appear verbatim in the whitelist section are not flagged. Owner-declared public material — company website, public contact email, partner names already announced publicly — no longer triggers `fatal_alerts.jsonl` entries.

**Pre-v1.4.3 JELabs example**: owner asked SecondEvie about her own website (`jelabs.top`); the reply contained the company's public metrics (`100+`, `1000+`, `30+`). L4 observer flagged it as `layer_4_output_leak` with empty `name_leakage` / `pii` lists — a "long looks-like-marketing" heuristic. Noise the owner has to mentally filter.

**Migration**: add a section like the following to `~/.hermes/paid/sop.md` to declare a whitelist:

```
## 公开材料 / Public materials

- Website: https://yourcompany.com
- Contact email: you@yourcompany.com
- Public metrics: 100+ customers, 30+ countries
```

Whitelist is optional. Plugins on existing sop.md without that section behave exactly as before.

## Upgrade path

Backwards-compatible across the board:

- Markdown strip only fires when `platform in ("feishu", "lark")` — no behavior change for other platforms
- Length-cap rule lives in `_DIRECT_HARD_RULES_*` which are already always-included in direct-state context; the rule simply makes existing behavior expectations enforceable
- L4 whitelist is opt-in via sop.md section presence; absent section = pre-v1.4.3 behavior

## Tests

- 636 passing (+21 new vs v1.4.2's 615)
- New coverage: 9 markdown-strip patterns, 8 whitelist scenarios (yes-match / no-match / multiple-headings / missing-sop / pii-exact-match / cross-cp-skip / etc.), 3 hard-rule sanity checks

## Future work (still in backlog)

- v1.4.4 Tech debt + ops: wrap format unification, timer install docs, standalone send_dm fix
- v1.4.5 Architecture: per-cp `blacklist_action: "decline" | "request"` setting (retires the v1.4.1 surgical patch)
