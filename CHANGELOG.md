# Changelog

All notable changes to PAID are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning follows SemVer (see `~/.openclaw/workspace/GIT_VERSION_SCHEME.md`).

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
