"""i18n table for paid_review skill cp-facing replies (v1.5.3).

Three-language support — zh / en / ko — selected by `detect_lang()` from
the cp's input text. Tone goal across all languages: friendly senior
*asking* the cp for input, not a form to fill (Round 2 feedback).

Coverage: high-traffic reply strings only. Other internal warnings and
edge messages stay in zh — i18n expansion is incremental.

Adding a new language:
  1. Add lang key to ``REPLIES[*]`` for each template
  2. Add a detector branch in ``detect_lang()`` if needed
"""

from __future__ import annotations


_HAN_LO, _HAN_HI = 0x4E00, 0x9FFF        # CJK Unified Ideographs
_HANGUL_LO, _HANGUL_HI = 0xAC00, 0xD7AF   # Hangul Syllables
_KANA_LO, _KANA_HI = 0x3040, 0x30FF       # Hiragana + Katakana (lean Japanese, treated as zh fallback for now)


def detect_lang(text: str) -> str:
    """Return ``"zh"`` | ``"en"`` | ``"ko"`` based on the dominant script
    in *text*. Defaults to ``"zh"`` when input is empty or ambiguous
    (back-compat with v1.5.x which hardcoded zh).

    Heuristic — count chars in CJK Han, Hangul, and Latin alphabetic
    ranges. Whichever has the most chars wins, with thresholds to
    avoid one-letter triggers.
    """
    if not text:
        return "zh"
    cjk = sum(1 for c in text if _HAN_LO <= ord(c) <= _HAN_HI)
    hangul = sum(1 for c in text if _HANGUL_LO <= ord(c) <= _HANGUL_HI)
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    total = cjk + hangul + latin
    if total == 0:
        return "zh"
    # Korean signal is strong even with a few chars — Hangul rarely
    # appears in other languages.
    if hangul >= 2 and hangul / total > 0.2:
        return "ko"
    if cjk / total > 0.3:
        return "zh"
    if latin / total > 0.5:
        return "en"
    return "zh"


def t(key: str, lang: str = "zh", **fmt) -> str:
    """Look up a reply template by ``key`` + ``lang`` and substitute
    keyword arguments via :py:meth:`str.format_map`.

    Falls back to ``zh`` when *lang* is missing from the table, then
    to a placeholder if the key itself is unknown (should not happen
    if callers stick to defined keys).
    """
    table = REPLIES.get(key)
    if table is None:
        return f"[i18n: missing key {key!r}]"
    template = table.get(lang) or table.get("zh") or next(iter(table.values()))
    try:
        return template.format_map(_SafeDict(fmt))
    except Exception:
        return template


class _SafeDict(dict):
    """`str.format_map`-compatible dict that leaves unknown keys as-is
    (`{foo}` stays `{foo}`) instead of raising KeyError."""

    def __missing__(self, key):  # pragma: no cover - format edge
        return "{" + key + "}"


# Reply templates. Tone: questioning + guiding (Round 2 feedback). Avoid
# "请输入X" imperatives; prefer "想X的话Y" or "X 是?" style.
REPLIES: dict[str, dict[str, str]] = {
    # ---- INTAKE / SUBJECT ----
    "subject_ask": {
        "zh": "收到。这次 review 的主题，我理解成：\n\n**{top}**\n\n对吗？回 `yes` 我就开始；不对就把正确的主题打过来。\n（中途想退出发 `/review cancel`）{alt_hint}",
        "en": "Got it. I'm reading the subject as:\n\n**{top}**\n\nRight? Reply `yes` and I'll start. If not, just type the correct subject.\n(To bail out anytime: `/review cancel`){alt_hint}",
        "ko": "확인했어요. 이번 review 의 주제를 이렇게 잡았는데요:\n\n**{top}**\n\n맞나요? `yes` 로 답하면 바로 시작할게요. 아니면 정확한 주제를 입력해 주세요.\n(중간에 그만하고 싶으면 `/review cancel`){alt_hint}",
    },
    "subject_ask_alt_hint": {
        "zh": "\n\n_其它候选: {alts}_",
        "en": "\n\n_Other candidates: {alts}_",
        "ko": "\n\n_다른 후보: {alts}_",
    },
    "subject_ask_reprompt": {
        "zh": "还在等你确认主题：**{top}**\n回 `yes` 开始，或直接把正确的主题打过来。",
        "en": "Still waiting on the subject: **{top}**\nReply `yes` to start, or send me the right subject.",
        "ko": "주제 확인을 기다리고 있어요: **{top}**\n`yes` 로 시작하거나 정확한 주제를 보내주세요.",
    },
    "subject_no_candidates": {
        "zh": "我还没拿到 subject 候选 — 想 review 什么主题，直接打给我？",
        "en": "I don't have any subject candidates yet — what would you like reviewed? Type it in.",
        "ko": "아직 주제 후보가 없어요 — 어떤 내용을 review 하고 싶은지 알려주실래요?",
    },
    "subject_pass_legacy": {
        "zh": "把要 review 的主题直接打给我就行（不需要 pass / custom）。",
        "en": "Just type the subject you want reviewed (no need for pass / custom).",
        "ko": "review 하고 싶은 주제를 그냥 입력해 주세요 (pass / custom 같은 거 필요없어요).",
    },

    # ---- QA ----
    "qa_continue_pending": {
        "zh": "还有 {n} 条 finding 没回应，想继续看吗？",
        "en": "{n} finding(s) still pending — want to keep going?",
        "ko": "아직 답변하지 않은 finding 이 {n} 개 있어요 — 계속 볼까요?",
    },
    "qa_no_more_deferred": {
        "zh": "deferred finding 都拉完了。都看完了吗？回 `done` 我整理 brief。",
        "en": "No more deferred findings. All done? Reply `done` and I'll write up the brief.",
        "ko": "deferred finding 이 더 없네요. 다 보셨나요? `done` 으로 답하면 brief 정리할게요.",
    },
    "qa_done_prompt": {
        "zh": "所有 finding 处理完了。都看完了吗？回 `done` 我整理 brief；想再过一遍 deferred 的回 `(more)`。",
        "en": "All findings handled. Done reviewing? Reply `done` and I'll wrap up. To re-visit deferred items, reply `(more)`.",
        "ko": "모든 finding 처리 완료. 다 보셨다면 `done`, deferred 항목 다시 보려면 `(more)` 로 답해주세요.",
    },
    "qa_finding_missing": {
        "zh": "（finding 不见了，可能是状态有点乱 — 发 `/review cancel` 重开一轮？）",
        "en": "(Finding is missing — state may be off. Want to `/review cancel` and try again?)",
        "ko": "(finding 을 찾을 수 없어요. 상태가 꼬였을 수 있어요 — `/review cancel` 후 다시 해볼까요?)",
    },

    # ---- SCAN ----
    "scan_no_findings": {
        "zh": "扫描完了，没发现需要讨论的 finding —— 这份材料看起来 decision-ready。{partial_note}",
        "en": "Scan done — nothing needs discussion. The doc looks decision-ready.{partial_note}",
        "ko": "스캔 완료 — 논의가 필요한 항목 없어요. 자료가 결정 가능한 상태로 보여요.{partial_note}",
    },

    # ---- CANCEL ----
    "cancel_no_active": {
        "zh": "你目前没有进行中的 review session 可以取消。想 review 什么主题，直接发 `/review <主题>`。",
        "en": "You don't have an active review session to cancel. To start one, send `/review <subject>`.",
        "ko": "취소할 진행 중인 review session 이 없어요. 시작하려면 `/review <주제>` 를 보내주세요.",
    },
    "cancel_done": {
        "zh": "已关闭。想再来一轮 review，发 `/review <主题>` 就行。\n\n{detail}",
        "en": "Closed. To start another round, send `/review <subject>`.\n\n{detail}",
        "ko": "닫았습니다. 다시 review 시작하려면 `/review <주제>` 를 보내주세요.\n\n{detail}",
    },
    "review_already_active": {
        "zh": "你已经有一个进行中的 review session 了（{sid}）。先发 `/review cancel` 关掉它，再开新的。",
        "en": "You already have an active review session ({sid}). Send `/review cancel` first, then start a new one.",
        "ko": "이미 진행 중인 review session 이 있어요 ({sid}). 먼저 `/review cancel` 로 닫고 새로 시작하세요.",
    },
    "review_need_subject": {
        "zh": "想 review 什么主题？发 `/review <主题>` 给我。例: `/review Q3 OKR 草稿`",
        "en": "What would you like reviewed? Send `/review <subject>`. e.g. `/review Q3 OKR draft`",
        "ko": "어떤 주제를 review 할까요? `/review <주제>` 로 보내주세요. 예: `/review Q3 OKR 초안`",
    },
}
