# PAID v1.5.0 — Multimedia review + group support

**Released:** 2026-05-14

v1.5 turns paid-review from "junior pastes plaintext" into a full-fidelity
research-quality reviewer. The review skill now ingests Lark Docs, Lark
Wiki pages, PDFs, images (OCR), and arbitrary public web URLs. It also
opens PAID up to group chats — with explicit per-group opt-in so no bot
ever auto-replies in a group it was added to.

---

## What's new

### Multimedia review (T1 + T2)

Junior can now `/review` with:

- **Lark Docs and Wiki pages** — `https://*.feishu.cn/docx/...` or
  `https://*.larksuite.com/wiki/...` URLs are fetched via Lark Open API
  and inlined as plaintext for the owner brief.
- **PDFs** — pdftotext (poppler-utils) with pdfminer.six fallback;
  scanned PDFs surface as "image-based, OCR coming in v1.6".
- **Images** — tesseract OCR with `chi_sim+eng` default lang
  (`PAID_OCR_LANGS` env to override). Photos of whiteboards, screenshots
  with text, etc.
- **Web pages** — generic HTTP(S) URLs go through readability-lxml
  for article extraction; bs4 fallback if readability fails. Lark URLs
  always route to LarkDocBackend first.

Every backend graceful-degrades: missing dependencies produce a clear
advisory error in the brief's ⚠️ Ingest errors block rather than crashing
the review.

**SSRF defense** on web-scrape: hostnames are resolved and any private /
loopback / link-local / multicast / reserved IP rejects the fetch.
AWS 169.254.169.254 metadata, 10.x, 192.168.x, fc00::, fe80::, ::1
all blocked. Split-horizon DNS handled (any bad IP → reject).

### Group chats (opt-in)

PAID now safely runs in group chats. **By default, all group messages
are silently dropped** — owner must explicitly enable each group.

```
/paid-enable-group [mode]        # review-only (default) | everyday | both
/paid-disable-group              # keeps config, flips enabled=false
/paid-set-group-mode <mode>      # change later
/paid-set-group-name <name>      # display name shown in /paid-list-groups
/paid-group-status               # current group's config
/paid-list-groups                # all groups (works in DM)
```

Group-bound commands refuse to run in DMs. Owner `/paid-*` always
bypasses the group-routing gate so the owner can opt a freshly-added
group in from the group itself.

### Internal: paid.lark_client extraction

Phase 1 split the Lark Open API client out of the review skill into a
reusable `paid.lark_client` module — sync httpx, token caching, retry
on 401/429/5xx, lazy singleton via `get_lark_client()`. Used by the
review skill's LarkDocBackend; future modules (cron sweeps, dashboard)
will reuse it instead of re-implementing.

---

## Deploy

After upgrade on the VPS `paid` user:

```bash
# Required for new backends:
sudo apt install -y poppler-utils tesseract-ocr tesseract-ocr-chi-sim
pip install pdfminer.six pytesseract pillow httpx beautifulsoup4 readability-lxml lxml
```

Until installed, those backends graceful-degrade (advisory error in
brief, file still archived). Pilot stays functional throughout.

### Lark env (unchanged from v1.4):
```
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_GROUP_POLICY=open
FEISHU_ALLOW_BOTS=mentions
FEISHU_BOT_OPEN_ID=ou_...
```

### Optional:
```
PAID_OCR_LANGS=chi_sim+eng    # override default tesseract langs
```

---

## Test coverage

`830 passed, 1 skipped` — 89 new tests added over Phases 1-7:

- 24 LarkClient (Phase 1)
- 31 + 5 ingest backends + brief rendering (Phase 2)
- 12 PDF backend (Phase 3)
- 15 Image OCR backend (Phase 4)
- 30 web scrape backend (Phase 5; 1 skipped on readability not installed)
- 33 + 7 group routing core + hook integration (Phase 6)
- 20 group self-service slash commands (Phase 7)

---

## Phases (intermediate PRs)

Each phase was shipped as its own PR for forensic clarity:

- #11 Phase 1 — paid.lark_client extraction
- #12 Phase 2 — T1 multimedia (Lark Doc + Wiki URL backends, dispatcher rewrite)
- #13 Phase 3 — T2.PDF backend
- #14 Phase 4 — T2.image OCR backend
- #15 Phase 5 — T2.web scrape backend
- #16 Phase 6 — group routing core
- #17 Phase 7 — group self-service slash commands

---

## Backward compatibility

- v1 session JSON files still load (schema_version migration is
  default-tolerant: missing fields → `[]`).
- v1.4.x P2P pilots are unaffected: `classify_chat` returns `"p2p"`
  for any event whose source doesn't say `chat_type=group`, so the
  Phase 6 gate never fires for them.
- All existing slash commands (`/paid-pending`, `/paid-approve`, etc.)
  are unchanged. New `/paid-*-group` prefixes don't collide.
