# PAID v1.5.3 — UX polish from live manual test

**Released:** 2026-05-14
**Triggered by:** Round 1+2 v1.5 manual test on paid uid 1002 VPS

Six UX / DX fixes from issues found while testing v1.5.0 end-to-end live.
None of these is a correctness bug — they're all "this works but feels
wrong" issues a real cp would hit.

---

## What's in (all 6 items)

### #1 Subject confirmation prompt includes `/review cancel` hint

cp saw subject prompt with no obvious way to bail out mid-review. Now:

```
收到。这次 review 的主题，我理解成：
**Q3 OKR 草稿**
对吗？回 `yes` 我就开始；不对就把正确的主题打过来。
（中途想退出发 `/review cancel`）
```

### #3 Multi-language replies: zh / en / ko

cp-facing reply strings now flex by detected input language. Live test
used Chinese only; now an English-speaking cp sending `review the Q3
roadmap` gets English replies, Korean cp gets Korean.

- New module `paid_review/i18n.py`: `detect_lang()` + `t()` template
  lookup. ~11 reply keys in zh + en + ko.
- `SessionState.lang` field (schema v2 stays; new field defaults to
  `""` which means "detect at INTAKE").
- Other internal warnings / debug messages still zh — incremental
  expansion.

### #4 Replies use questioning / guiding tone

Reply tone goal (from user feedback): "感觉像一个 senior 在问，不是表格".

- `"请把要 review 的主题打出来"` → `"想 review 哪个主题？打给我"`
- `"对的话回 yes 我直接开始"` → `"这个主题对吗？回 yes 我开始"`
- `"回 done 完成 QA"` → `"都看完了吗？回 done 我整理 brief"`

Rolled out across `subject_ask`, `qa_continue_pending`, `qa_done_prompt`,
`qa_no_more_deferred`, `cancel_done` templates in all 3 languages.

### #5 Cancel command accepts synonyms

Previously only `/review cancel` and `/r cancel` exact-matched. Common
alternatives like `close`, `stop`, `abort`, `end`, `exit`, `quit`
opened *new* review sessions with the word as content (real bug observed
during test). Now all of these force-close the active session:

```
/review cancel   /review close   /review stop   /review abort
/review end      /review exit    /review quit
/r cancel        /r close        /r stop        /r abort
/r end           /r exit         /r quit
```

### #6 `/review` regex tolerates CJK directly after prefix

Chinese-speaking cp typed `/review看一下我的资料` (no space, CJK directly
follows). Previously the regex required whitespace/EOL after `/review`,
so the message routed as chatter and was dropped. Now:

- `^/r(?:eview)?(?:$|[^a-zA-Z])` — accepts any non-letter (CJK,
  punctuation, digits) immediately after prefix
- `/reviewing`, `/reviewers` still NOT recognized (alpha continuation
  is a different word)

Affected:
- `paid/group_routing.py::_REVIEW_CMD_RE`
- `__init__.py::on_pre_gateway_dispatch::is_review_cmd` helper

### #7 `/review` in a group → reply routes back to the group

Previously when cp sent `/review` in a Lark group, PAID's reply went to
the cp's *DM with the bot* (silent from the group's POV — cp thought
nothing happened). Now PAID detects `event.source.chat_type=="group"`
and replies to the group's `chat_id`.

`_handle_review_in_pre_gateway` gained a new `event=` kwarg; when given,
extracts chat_id + chat_type and routes the reply target accordingly.
P2P DM behavior unchanged. Existing callers that don't pass `event`
keep DM behavior (backward compat).

---

## Deferred to v1.5.4

### #2 Attachment binding (PDF / Image OCR via Lark)

Lark delivers `/review` text and image attachment as 2 separate events.
PAID's INTAKE finalizes on the first event (no attachment), so
`ImageBackend` / `PdfBackend` never receive the file. The fix requires:

- In-memory buffer of recent inbound media per cp
- Pull buffered attachments on `/review` arrival (within TTL)
- Tested in a clean live e2e

This is genuine ~half-day work plus a careful manual test against
real Lark uploads. Deferred to keep v1.5.3 tight + ship the high-value
items tonight.

---

## Tests

`tests/test_v1_5_3_fixes.py` — **27 new tests**:
- 5 `detect_lang` (zh / en / ko / empty / mixed-with-URL)
- 5 `t()` template lookup (lang variants + kwargs + fallback)
- 10 parametrized cancel-synonyms
- 4 review-regex CJK tolerance + alpha-continuation guards
- 3 group-reply routing (group→group, dm→dm, no-event→dm)

Plus 2 legacy tests in `test_review_plugin_glue.py` updated to accept
the new i18n variants.

`870 passed, 1 skipped` — full suite green. +27 net vs v1.5.2.

---

## Deploy

Same dep surface as v1.5.x — no new apt or pip deps.
