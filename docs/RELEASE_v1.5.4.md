# PAID v1.5.4 — Lark attachment binding fix

**Released:** 2026-05-14
**Triggered by:** 2026-05-14 live manual test of v1.5 — Image OCR and
PDF backends never fired because of Lark's two-event delivery model.

---

## The bug

Lark Open Platform delivers `/review <text>` and an attached image
(or PDF) as **two separate inbound events**, 1-10 seconds apart:

```
T+0.0s: text='/review 看一下我的资料'   message_type=command   media_urls=[]
T+4.0s: text=''                       message_type=photo     media_urls=['/cache/abc.jpg']
```

PAID v1.5.0-v1.5.3 finalize INTAKE at T+0 — the second event arrives
to an already-initialized session and was routed to hermes' main agent
(Claude vision pre-analyze), never reaching PAID's `ImageBackend` or
`PdfBackend`. The session's `ingest_sources` only contained the text;
the image silently fell out of the review flow.

Reproduced live during 2026-05-14 Round 1c manual test on paid uid 1002
VPS. ImageBackend was correctly installed (tesseract present, pytesseract
imports, `has_extractor=True`), but never invoked.

## The fix

PAID v1.5.4 adds bidirectional binding for the two-event delivery:

### Order A — `/review` first, image later

1. `/review` event opens session with `attachments=[]` (unchanged).
2. Image event arrives. `on_pre_gateway_dispatch`:
   - cp has `active_review_session` set
   - event has `media_urls` but no `/review` text prefix
   → calls new `paid_review.api.add_attachments_to_session(sid, ...)`,
   which re-runs the ingest dispatcher on the new attachment, appends
   the extracted text to the session's `normalized.md`, and extends
   `state.ingest_sources` / `state.ingest_errors`.
   - returns `{"action": "skip", "reason": "paid_review_attachment_bound"}`

### Order B — image first, `/review` later

1. Image arrives. cp has no active session. `on_pre_gateway_dispatch`:
   - Adds the media path to new `paid_review.attachment_buffer` (in-memory
     per-cp, TTL 90s, cap 8 per cp).
   - Returns `None` (no skip) so hermes continues normal handling — the
     buffer is a passive memo; we don't intercept hermes' vision flow.
2. `/review` arrives. `_maybe_route_to_review_skill` drains the buffer
   for this cp and passes the buffered paths to `intake()` as
   `attachments`. ImageBackend now fires.

### TTL & cleanup

- Default TTL 90s (Lark's 2-event gap is typically 1-5s; 90s gives wide
  margin for cp pause).
- Inline prune on every add/drain — buffer cannot grow unbounded.
- Max 8 attachments per cp (oldest evicted on add).
- Owner-side media bypasses the buffer entirely (owners use J0 vision
  flow, not review skill).

## New API

```python
paid_review.api.add_attachments_to_session(sid: str, attachments: list[dict]) -> dict
```

Append attachments to an active session. Returns:

```python
{
    "ok": bool,
    "added_sources": int,   # new ingest_sources entries
    "added_errors": int,    # new ingest_errors entries
    "appended_chars": int,  # how much normalized.md grew
    "reason": str,          # set when ok=False
}
```

Refuses on CLOSED/missing sessions. Re-runs the standard dispatcher
which handles all backends uniformly (text, lark_doc, pdf, image,
web_scrape).

## New module

`paid_review/attachment_buffer.py` — thread-safe per-cp buffer:
- `add(platform, sender_id, path=..., mime=..., name=...)`
- `drain(platform, sender_id) → list[dict]` (clear-and-return)
- `peek(platform, sender_id)` (read-only, for tests + diagnostics)
- `clear(platform=None, sender_id=None) → int` (test helper)

## Tests

`895 passed, 1 skipped` — full suite green. +25 net vs v1.5.3:

- `tests/test_attachment_buffer.py` — 13 tests: add/drain/TTL/cap/clear/
  isolation between cps + platforms.
- `tests/test_review_add_attachments.py` — 7 tests: happy path + refuse
  (missing session, CLOSED, empty attachments), QA-stage still allowed,
  missing file path produces breadcrumb error.
- `tests/test_v1_5_4_attachment_binding.py` — 5 hook-integration tests:
  Order A (active session binding), Order B (buffer → drain at intake),
  owner-side media not buffered, text+media single-event bypasses
  buffer branch.

## Hermes adapter behavior we rely on

`gateway/platforms/base.py::MessageEvent`:
- `text: str` (empty for media-only)
- `message_type: MessageType` (TEXT / PHOTO / DOCUMENT / AUDIO / VIDEO)
- `media_urls: List[str]` — **local file paths** (hermes downloads
  Lark images via `_download_feishu_image` and caches to disk before
  firing the event).
- `media_types: List[str]` — parallel mime types.

We pull `media_urls` and `media_types` directly. No additional Lark API
calls. Buffer holds these paths; dispatcher's existing file-routing
logic does the work.

## Deploy

Same dep surface as v1.5.x — no new apt or pip deps.

Standard deploy: rsync to VPS paid user, restart hermes-gateway.

## Manual verification on VPS

After deploy:
1. Have Evie send `/review 看一下` + an image in same Lark chat send.
2. Expect within 10s: `[review attach] bound N to active sid=...` in
   plugin_runtime.log.
3. Inspect session meta.json: `ingest_sources` should have BOTH a
   `text:message` entry AND an `image:...png` entry.
4. Session brief eventually delivered to owner with `## Sources`
   listing `<path> via image`.

If Evie sends image FIRST then `/review`:
1. Expect: `[review attach] buffered N media for cp=...` immediately
   after the image.
2. Then on `/review`: `[review attach] drained N buffered media for
   cp=...` and intake runs with both.
