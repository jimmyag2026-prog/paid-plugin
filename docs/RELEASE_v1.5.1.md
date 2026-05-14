# PAID v1.5.1 — Audit-fix patch

**Released:** 2026-05-14
**Triggered by:** v1.5.0 full-system audit (2026-05-14)

Patch release fixing the 3 highest-signal findings from the v1.5.0
audit. Other audit items deferred to v1.6 are listed below for the
record.

---

## Fixes

### 🔴 Critical #5 — review-only group mode no longer auto-replies to chatter

**Symptom (v1.5.0):** A junior types "lunch?" in a group enabled with
`/paid-enable-group review-only`. The routing layer returned
`group_review` for every message (commands and chatter alike), and the
caller fell through to the existing P2P logic — so the bot ran
classification + LLM and replied. Defeats the purpose of "review-only".

**Fix:** `classify_routing()` now returns a new state `group_review_strict`
for non-command messages in review-only groups. The hook `on_pre_gateway_dispatch`
checks whether the sender has an active review session; if not, the
message is dropped with reason `paid_group_review_only_non_review_message`.

Commands (`/review`, `/r`, `/paid-*`) and active-session QA continuation
still work.

### 🔴 High #1 — SSRF redirect bypass closed

**Symptom (v1.5.0):** `WebScrapeBackend` passed `follow_redirects=True`
to httpx, which would follow a 302 from a public host to an internal
IP without re-running the SSRF guard. The initial `_is_safe_host` check
was only applied to the first URL.

**Fix:** Replaced httpx's redirect handling with a manual loop
(`_fetch_with_ssrf_aware_redirects`). On every hop:
- Refuse non-http(s) targets (defeats `file://`, `javascript:` redirects)
- Re-resolve the host through `socket.getaddrinfo` and run the IP
  classifier — refuses if any A/AAAA points at loopback / private /
  link-local / multicast / reserved / unspecified
- Resolve relative `Location` headers via `urljoin`
- Detect redirect loops (URL appears twice in chain → refuse)
- Cap at `_MAX_REDIRECTS=3` hops

### 🟡 Medium #6 — OCR empty-result error names `PAID_OCR_LANGS`

**Symptom:** When tesseract returns 0 chars, the error said only
"image may have no readable text". Owners running OCR on Japanese /
Korean / French content had no signal that the issue might be the
default `chi_sim+eng` lang pack.

**Fix:** Error message now lists the active langs and tells the owner
exactly how to override:

```
tesseract extracted 0 chars (langs=chi_sim+eng) — image may have no
readable text, contrast too low, OR the language pack is wrong.
Override via env: PAID_OCR_LANGS=eng (English only), jpn+eng, kor+eng,
fra+eng, etc. Install lang packs with `apt install tesseract-ocr-<lang>`.
(Vision-LLM fallback planned for v1.6.)
```

---

## Audit findings deferred to v1.6

| # | Finding | Why deferred |
|---|---|---|
| High #4 | Group no @-mention check | Only matters in `everyday`/`both` mode; v1.5 explicitly drops non-command chatter in those modes via caller-side gate. v1.6 will wire the proper Claude flow + @-mention semantic together. |
| High #3 | Fake-LLM call_count test pattern | Pre-existing test infrastructure debt — not a v1.5 regression. v1.6 fake_llm rewrite. |
| High #C2 | QA cursor concurrent write | Single-user pilot rarely triggers; only multi-device same-cp concurrent QA can race. v1.6 adds flock around `annotations.jsonl` + `cursor.json`. |
| Medium #C1 | `set_active_review_session` direct-call lock | All production callers go through `api.intake()` which IS flocked. v1.6 wraps the helper. |
| Medium #1 | QA finding rollback | UX nice-to-have; doesn't affect correctness. v1.6 `/back` command. |
| Medium #C3 | SessionState multi-thread race | Single-thread gateway in current pilots; v1.6 if multi-worker becomes a thing. |

---

## Tests

11 new tests in `tests/test_v1_5_1_audit_fixes.py`:

- review-only chatter dropped when sender has no active session
- review-only chatter allowed when sender HAS active session (QA continuation)
- review-only `/review` command still routes
- review-only owner `/paid-*` still routes
- SSRF redirect to internal IP blocked at the redirect hop
- SSRF chain of public hosts followed correctly
- SSRF redirect loop detected
- SSRF non-http(s) redirect target blocked
- SSRF relative-URL redirect resolves against origin
- OCR empty error mentions `PAID_OCR_LANGS`
- OCR empty error mentions current lang setting

`841 passed, 1 skipped` — full suite green. +11 net vs v1.5.0.

---

## Deploy

Same dependency surface as v1.5.0 — no new apt or pip deps.

```bash
# After git pull / rsync:
sudo -u paid -H -- systemctl --user restart hermes-gateway.service
```
