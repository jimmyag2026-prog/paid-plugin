# Changelog

All notable changes to PAID are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows SemVer (see `~/.openclaw/workspace/GIT_VERSION_SCHEME.md`).

## [Unreleased]

### Fixed
- Upstream hermes-agent treats `"@_all" in raw_content` as a bot
  mention, so any Lark group `@所有人` broadcast passed
  `require_mention` and woke PAID. New idempotent patch script
  `scripts/patches/feishu_atall_not_bot_mention.py` removes that
  early-return — real `@bot` and `@bot + @all` still admit through
  the mentions[] open_id check; `@all` alone is now silently dropped
  upstream of PAID.

## [1.6.19] - 2026-05-21

JE Labs pilot feedback (owner Evie): Lark group `@bot` and DM replies
arrived as plain unformatted text — **bold** rendered literally,
markdown tables had proportional-font misaligned columns. Two root
causes traced + fixed.

### Fixed

- `paid/hermes_io.py`: removed pre-emptive `_strip_markdown_for_lark`
  call inside `send_dm` for `feishu`/`lark` platforms. The v1.4.3 strip
  was a workaround for an older hermes that only sent `text` msg_type;
  current hermes-agent's `_build_outbound_payload` auto-routes markdown
  to Lark's `post` msg_type which renders bold/headings/lists/links
  natively. Stripping first cancelled the rich rendering. The
  `_strip_markdown_for_lark` helper itself is kept available for
  callers that explicitly want plain text (audit summaries, console
  diagnostics).
- `scripts/patch_hermes_feishu_rich_text.py`: new **idempotent**
  deploy-time patch for hermes-agent's `gateway/platforms/feishu.py`.
  Pre-patch the adapter forced plain `text` whenever a markdown table
  was present, killing bold/headings AND leaving the table itself
  misaligned. Post-patch the adapter wraps the table block in
  ``` fences (Lark post `md` renders fenced content as monospace →
  aligned columns) while keeping the rest of the message in post mode
  so other formatting survives. Marker comment lets re-run skip; backup
  + AST validation on apply; rollback on syntax failure. Apply with
  `python3 scripts/patch_hermes_feishu_rich_text.py` after each hermes
  upgrade until the change is upstreamed.

### Notes

- Owner pass-through path (XiaEvie's own messages in group/DM) only
  benefits from the hermes-side patch — PAID's `send_dm` isn't on that
  path. Other-counterparty replies go through both paths and benefit
  from both fixes.
- No version SSOT drift: `bin/bump-version.sh` updates `_version.py` +
  `plugin.yaml` in lockstep; `test_version_sync.py` 4/4.

## [1.6.11] - 2026-05-15

Catch-up release: bundles all merged work since `v1.6.10` (the
commit-subject labels v1.6.11–v1.6.18 are internal patch markers; this
single tag covers the whole batch). plugin.yaml version corrected from
the drifted `1.6.0` to `1.6.11`.

### Added
- Group `everyday`/`both` modes now wire the Claude flow — a
  non-command @-mention group message routes through the same J2 cp
  pipeline a P2P DM uses (approval card to owner DM, reply to group).
  Pre-fix it was silently dropped as `paid_group_mode_reserved_*`.
  (v1.6.17, PR #42)
- Lark Drive `/file/<token>` URLs are now ingestible — `LarkDocBackend`
  downloads via `LarkClient.download_file` and re-routes through the
  PDF/image/text file backends by mime. (v1.6.18, PR #42)
- Ingest-failure UX gate: when a link can't be fetched the first
  reviewer turn asks "continue / `/review cancel`" instead of silently
  running SCAN/QA on empty input. (v1.6.15b, PR #42)
- `scripts/doctor.py` import-checks ingest deps (bs4/readability/lxml/
  httpx) + warns on missing pdftotext/tesseract; INSTALL.md lists them.
  (v1.6.15, PR #42)
- conv_capture happy-path observability logging in
  `on_pre_gateway_dispatch`. (v1.6.12, PR #39)

### Fixed
- `_route_urls_in_text` no longer silently drops a URL when the backend
  failed/returned empty — the URL stays inline annotated
  `[未能读取此链接（<reason>）: <url>]` so review never runs on a
  vanished link. (v1.6.15, PR #42)
- `WebScrapeBackend` detects anti-scrape / JS-wall placeholder shells
  (x.com "JavaScript is disabled", Cloudflare interstitial, EN+ZH) and
  rejects them instead of feeding the shell to the reviewer.
  (v1.6.15, PR #42)
- Classifier prompt carve-out: a broad allowed-topic label (e.g.
  `logistics`) no longer authorizes answering internal
  people-management questions (employee WFH/leave/comp/headcount/perf)
  — these escalate unless the SOP has an explicit policy.
  (v1.6.14, PR #42)
- `_parse_proposals` merges list-typed profile fields instead of
  overwriting — accepting a conv_capture proposal of `["X"]` no longer
  wipes a 3-item `always_decline` down to one. (v1.6.11, PR #38)

### Skipped
- conv_capture trigger-regex blind spot for conditional phrasing
  ("如果X 直接拒绝") — deferred per owner; tracked in
  `paid-may/design/05_backlog.md` v1.6.13.
