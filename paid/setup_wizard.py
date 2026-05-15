"""Module W — interactive /paid-setup wizard state machine (v1.6.0).

Owner-side DM-based onboarding flow. When owner types ``/paid-setup``,
PAID intercepts in ``on_pre_gateway_dispatch`` and starts a 5-question
sequence. The owner's next 5 plain-text DM messages are captured as
answers (NOT routed to hermes / Claude).

State is module-level dict keyed by ``(platform, owner_user_id)``, with
TTL=15 min so a half-finished wizard auto-cancels. Similar pattern to
``_AWAITING_INPUT`` (J3 ✏️ reply mode), but separate registry — wizard
has its own multi-step semantics.

After Q5 (or owner types ``/paid-setup cancel``), the wizard:
  1. Loads existing profile or makes new one from owner answers
  2. Applies the answers to profile fields
  3. Calls ``paid.profile_sync.derive_from_profile()`` to regenerate
     legacy files (owner.json / persona.md / sop.md / settings.json)
  4. Sends a confirmation summary to owner DM
  5. Clears wizard state

Edit mode: when owner DMs ``/paid-setup`` and a profile already exists,
the wizard shows a summary + edit menu instead of running the full 5Q.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import profile as _profile
from . import profile_sync as _profile_sync

logger = logging.getLogger(__name__)


_WIZARD_TTL_SEC = 15 * 60  # 15 min — owners should finish fast or restart
_LOCK = threading.Lock()
_WIZARD_STATE: dict[tuple[str, str], "WizardState"] = {}


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class WizardState:
    """Per-owner wizard progress."""
    platform: str
    owner_id: str
    mode: str = "first_time"  # "first_time" | "edit" | "doc_confirm"
    step: int = 0              # 0 means just started; 1-5 means waiting for that question's answer
    answers: dict[str, Any] = field(default_factory=dict)
    edit_field: str = ""       # when mode="edit", which single field is being edited
    since_ts: float = 0.0
    # v1.6.1: pending doc ingest proposals
    doc_proposals: list = field(default_factory=list)   # list[UpdateProposal]
    doc_url: str = ""
    # v1.7.0: post-Q5 follow-up. True iff the wizard is waiting for the
    # owner to answer "which channel is your primary?" after the standard
    # 5 questions completed. Only fires when ≥2 enabled identities exist
    # AND profile.preferred_platform is not already set.
    awaiting_preferred_platform: bool = False


# ---------------------------------------------------------------------------
# Question definitions — first-time path (5 questions)
# ---------------------------------------------------------------------------


_QUESTIONS = [
    {
        "key": "name",
        "prompt": (
            "👋 PAID 5 题快速 setup（任何时候发 `/paid-setup cancel` 退出）\n\n"
            "1/5 你的名字？（counterparty 看到的称呼）"
        ),
    },
    {
        "key": "voice_preset",
        "prompt": (
            "2/5 语气偏好？回数字或名字：\n"
            "  1. founder  — 直接、简短、工程师风\n"
            "  2. professional — 正式不冷\n"
            "  3. casual — 口语化、可用表情\n"
            "  4. minimal — 最短回答、不寒暄"
        ),
    },
    {
        "key": "always_escalate",
        "prompt": (
            "3/5 哪些话题必须发给你审批？逗号分隔，比如：\n"
            "  `薪资, 招聘, 客户, 投资`\n"
            "回 `default` 用 v1.4 默认 5 类（equity, salary, hiring, customer, finance）"
        ),
    },
    {
        "key": "preferred_language",
        "prompt": (
            "4/5 主要回复语言？\n"
            "  1. auto — 跟 cp 的输入语言走（推荐）\n"
            "  2. zh — 中文\n"
            "  3. en — English\n"
            "  4. ko — 한국어"
        ),
    },
    {
        "key": "daily_cost_cap_usd",
        "prompt": (
            "5/5 每天最多花多少 LLM cost (USD)？\n"
            "  数字（例 5, 10, 20）或 `inf` 不设上限\n"
            "（默认 $5/day，超过 PAID 自动短路所有 LLM 调用）"
        ),
    },
]


# Map answer key → preset menus when the answer is a numeric pick
_PICK_MENUS = {
    "voice_preset": {
        "1": "founder", "2": "professional", "3": "casual", "4": "minimal",
        "founder": "founder", "professional": "professional",
        "casual": "casual", "minimal": "minimal",
    },
    "preferred_language": {
        "1": "auto", "2": "zh", "3": "en", "4": "ko",
        "auto": "auto", "zh": "zh", "en": "en", "ko": "ko",
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_active(platform: str, owner_id: str) -> bool:
    """True iff the owner is currently in the middle of a wizard.
    Auto-prunes expired entries."""
    if not platform or not owner_id:
        return False
    _prune_expired()
    return (platform, owner_id) in _WIZARD_STATE


def start(platform: str, owner_id: str) -> str:
    """Begin a wizard. Returns the first reply text PAID should DM owner.

    - First-time (no profile exists): returns Q1.
    - Edit mode (profile exists): returns summary + edit menu.
    """
    if not platform or not owner_id:
        return "PAID setup: platform/owner_id missing — can't start wizard."
    _prune_expired()

    existing = _profile.load_profile()
    state = WizardState(
        platform=platform,
        owner_id=owner_id,
        mode="edit" if existing else "first_time",
        step=0 if not existing else -1,  # -1 means "showing edit menu, awaiting field choice"
        since_ts=time.time(),
    )
    with _LOCK:
        _WIZARD_STATE[(platform, owner_id)] = state

    if existing:
        return _render_edit_menu(existing)
    # First-time → ask Q1
    state.step = 1
    return _QUESTIONS[0]["prompt"]


def consume(platform: str, owner_id: str, answer: str) -> tuple[str, bool]:
    """Process owner's next text reply within an active wizard.

    Returns ``(reply_text, done)``:
      - ``reply_text``: what to DM owner next (next question, summary, or
        confirmation).
      - ``done``: True iff wizard concluded (state cleared).

    Caller decides whether to also send ``reply_text`` via send_dm.
    """
    if not is_active(platform, owner_id):
        return ("PAID setup: no active wizard. Send /paid-setup to start.", True)

    key = (platform, owner_id)
    state = _WIZARD_STATE[key]
    state.since_ts = time.time()
    answer_str = (answer or "").strip()

    # Universal cancel
    if answer_str.lower() in ("/paid-setup cancel", "cancel", "/cancel", "退出"):
        with _LOCK:
            _WIZARD_STATE.pop(key, None)
        return ("PAID setup 已取消。Profile 没改。", True)

    # ---- Edit-mode flow ----
    if state.mode == "edit":
        return _consume_edit(state, answer_str)

    # ---- First-time 5Q flow ----
    return _consume_first_time(state, answer_str)


def cancel(platform: str, owner_id: str) -> str:
    """External cancel (e.g. owner runs `/paid-setup cancel` directly)."""
    key = (platform, owner_id)
    with _LOCK:
        _WIZARD_STATE.pop(key, None)
    return "PAID setup 已取消。"


# ---------------------------------------------------------------------------
# Resync — owner-triggered derive() without a wizard
# ---------------------------------------------------------------------------


def resync() -> str:
    """Re-render legacy files from current profile. No questions asked.
    Used by ``/paid-resync`` slash command."""
    prof = _profile.load_profile()
    if prof is None:
        return (
            "PAID 还没有 owner_profile.json — 发 `/paid-setup` 走 5 题 setup 流程。"
        )
    audit = _profile_sync.derive_from_profile(prof)
    return (
        f"✓ 重新生成了 {len(audit['wrote'])} 个 derived 文件: "
        + ", ".join(audit["wrote"])
    )


def start_doc_ingest(platform: str, owner_id: str, url: str) -> str:
    """v1.6.1: Fetch a doc URL, extract profile update proposals, store for confirm.

    Returns a DM-ready message to send to the owner (either the confirm prompt
    or an error/empty message).  Does NOT start a first-time wizard — works
    independently of wizard state.
    """
    from . import doc_ingest as _di

    prof = _profile.load_profile()
    if prof is None:
        return (
            "还没有 owner_profile.json — 先发 `/paid-setup` 完成初始 setup。"
        )

    try:
        _kind, content = _di.fetch_content(url)
    except ValueError as e:
        return f"⚠️ 无法获取文档内容：{e}"

    proposals = _di.extract_profile_updates(content, prof)
    if not proposals:
        # Still store the reference even if no structural updates
        _add_reference(prof, url, _kind, [])
        _profile.save_profile(prof)
        return (
            f"🔍 读取了 {url} 但没有找到可更新的 profile 字段。\n"
            "文档 URL 已加入 references 列表。"
        )

    # Store state for owner confirm
    with _LOCK:
        state = _WIZARD_STATE.get((platform, owner_id))
        if state is None or state.mode not in ("doc_confirm",):
            state = WizardState(
                platform=platform,
                owner_id=owner_id,
                mode="doc_confirm",
                since_ts=time.time(),
            )
            _WIZARD_STATE[(platform, owner_id)] = state
        state.doc_proposals = proposals
        state.doc_url = url
        state.since_ts = time.time()

    return _di.format_confirm_prompt(proposals)


def consume_doc_confirm(platform: str, owner_id: str, reply: str) -> tuple[str, bool]:
    """v1.6.1: Owner replied to the doc-confirm prompt. Apply accepted proposals.

    Returns (message, done). done=True clears the doc_confirm state.
    """
    from . import doc_ingest as _di

    _prune_expired()
    with _LOCK:
        state = _WIZARD_STATE.get((platform, owner_id))

    if state is None or state.mode != "doc_confirm":
        return ("没有待确认的文档更新。", True)

    proposals = state.doc_proposals
    accepted_indices = _di.parse_confirm_reply(reply, len(proposals))
    for i, prop in enumerate(proposals):
        prop.accepted = (i + 1) in accepted_indices

    prof = _profile.load_profile()
    if prof is None:
        with _LOCK:
            _WIZARD_STATE.pop((platform, owner_id), None)
        return ("profile 丢失，请重新 `/paid-setup`。", True)

    _di.apply_proposals(prof, proposals)

    # Add reference URL to profile
    accepted_topics = [
        tok
        for p in proposals if p.accepted and p.field == "topics.always_escalate"
        for tok in (p.proposed if isinstance(p.proposed, list) else [])
    ]
    _add_reference(prof, state.doc_url, "doc", accepted_topics)
    _profile.save_profile(prof)

    n_accepted = sum(1 for p in proposals if p.accepted)
    n_total = len(proposals)
    with _LOCK:
        _WIZARD_STATE.pop((platform, owner_id), None)

    return (
        f"✓ 接受了 {n_accepted}/{n_total} 条更新，profile 已保存并重新生成 derived 文件。",
        True,
    )


def is_doc_confirm_active(platform: str, owner_id: str) -> bool:
    """True iff owner is waiting to confirm a doc ingest proposal list."""
    _prune_expired()
    with _LOCK:
        state = _WIZARD_STATE.get((platform, owner_id))
    return state is not None and state.mode == "doc_confirm"


def _add_reference(prof: Any, url: str, kind: str, extracted_topics: list) -> None:
    """Append/update a reference entry in profile.references[]."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Dedup by URL
    for ref in prof.references:
        if isinstance(ref, dict) and ref.get("url") == url:
            ref["last_synced_at"] = now
            ref["extracted_topics"] = extracted_topics
            return
    prof.references.append({
        "kind": kind,
        "url": url,
        "label": "",
        "last_synced_at": now,
        "extracted_topics": extracted_topics,
    })


# ---------------------------------------------------------------------------
# First-time path consume
# ---------------------------------------------------------------------------


def _consume_first_time(state: WizardState, answer: str) -> tuple[str, bool]:
    # v1.7.0: post-Q5 follow-up — owner is replying to Q6 (preferred_platform).
    if state.awaiting_preferred_platform:
        return _consume_preferred_platform_answer(state, answer)

    qdef = _QUESTIONS[state.step - 1]
    parsed = _parse_answer(qdef["key"], answer)
    if isinstance(parsed, str) and parsed.startswith("ERR:"):
        # Re-prompt with hint, don't advance step
        return (f"{parsed[4:]}\n\n{qdef['prompt']}", False)
    state.answers[qdef["key"]] = parsed

    # Advance
    if state.step < len(_QUESTIONS):
        state.step += 1
        next_q = _QUESTIONS[state.step - 1]
        return (next_q["prompt"], False)

    # All 5 answered — ask Q6 if multi-channel, else finalize directly.
    return _maybe_ask_preferred_platform(state)


def _consume_edit(state: WizardState, answer: str) -> tuple[str, bool]:
    """Edit-mode: owner picks which field to edit, then we ask that one
    question, apply, re-derive, summary."""
    existing = _profile.load_profile()
    if existing is None:
        # Shouldn't happen since mode='edit' implies profile existed at start
        with _LOCK:
            _WIZARD_STATE.pop((state.platform, state.owner_id), None)
        return (
            "PAID setup: profile 不见了。发 /paid-setup 重新走 first-time 流程。",
            True,
        )

    if not state.edit_field:
        # Owner is responding to the edit menu — pick a field
        choice = answer.lower().strip()
        # v1.7.0: #6 is primary-channel when multi-channel, else "done".
        multi_channel = len(_enabled_platforms(existing)) >= 2
        if multi_channel:
            field_map = {
                "1": "name", "name": "name",
                "2": "voice_preset", "voice": "voice_preset", "tone": "voice_preset",
                "3": "always_escalate", "topics": "always_escalate", "escalate": "always_escalate",
                "4": "preferred_language", "language": "preferred_language", "lang": "preferred_language",
                "5": "daily_cost_cap_usd", "cost": "daily_cost_cap_usd", "cap": "daily_cost_cap_usd",
                "6": "preferred_platform", "primary": "preferred_platform",
                "主频道": "preferred_platform", "channel": "preferred_platform",
                "7": "done", "done": "done", "save": "done", "退出": "done",
            }
            menu_hint = "1=名字, 2=语气, 3=话题, 4=语言, 5=成本, 6=主频道, 7=保存退出"
            range_hint = "1-7"
        else:
            field_map = {
                "1": "name", "name": "name",
                "2": "voice_preset", "voice": "voice_preset", "tone": "voice_preset",
                "3": "always_escalate", "topics": "always_escalate", "escalate": "always_escalate",
                "4": "preferred_language", "language": "preferred_language", "lang": "preferred_language",
                "5": "daily_cost_cap_usd", "cost": "daily_cost_cap_usd", "cap": "daily_cost_cap_usd",
                "6": "done", "done": "done", "save": "done", "退出": "done",
            }
            menu_hint = "1=名字, 2=语气, 3=话题, 4=语言, 5=成本, 6=保存退出"
            range_hint = "1-6"
        picked = field_map.get(choice)
        if picked is None:
            return (f"请回 {range_hint} 选择字段({menu_hint})", False)
        if picked == "done":
            with _LOCK:
                _WIZARD_STATE.pop((state.platform, state.owner_id), None)
            return ("PAID setup 完成。", True)
        state.edit_field = picked
        if picked == "preferred_platform":
            # Custom prompt (not in _QUESTIONS — depends on owner's actual
            # enabled identities)
            return (
                _render_preferred_platform_question(_enabled_platforms(existing)),
                False,
            )
        qdef = next(q for q in _QUESTIONS if q["key"] == picked)
        return (qdef["prompt"], False)

    # Owner is responding to a specific field's question
    # v1.7.0: preferred_platform has its own parser (depends on enabled list)
    if state.edit_field == "preferred_platform":
        enabled = _enabled_platforms(existing)
        raw = (answer or "").strip().lower()
        if not raw:
            return ("空回复 — 回平台名或数字。", False)
        auto_idx = str(len(enabled) + 1)
        if raw in ("auto", "skip", "自动", auto_idx):
            existing.preferred_platform = ""
            picked = "_auto_"
        elif raw.isdigit() and 1 <= int(raw) <= len(enabled):
            existing.preferred_platform = enabled[int(raw) - 1]
            picked = existing.preferred_platform
        else:
            canonical = {"feishu": "lark", "lark": "lark", "telegram": "telegram",
                         "tg": "telegram", "slack": "slack"}
            cand = canonical.get(raw, raw)
            if cand not in enabled:
                return (
                    f"无法识别 {answer!r}。请回 1-{len(enabled)+1} 或平台名 "
                    f"({', '.join(enabled)} / auto)。",
                    False,
                )
            existing.preferred_platform = cand
            picked = cand
        _profile.save_profile(existing)
        _profile_sync.derive_from_profile(existing)
        state.edit_field = ""
        return (
            f"✓ 主频道已设为 {picked}。\n\n{_render_edit_menu(existing)}",
            False,
        )

    parsed = _parse_answer(state.edit_field, answer)
    if isinstance(parsed, str) and parsed.startswith("ERR:"):
        qdef = next(q for q in _QUESTIONS if q["key"] == state.edit_field)
        return (f"{parsed[4:]}\n\n{qdef['prompt']}", False)

    # Apply to existing profile
    _apply_answer(existing, state.edit_field, parsed)
    _profile.save_profile(existing)
    _profile_sync.derive_from_profile(existing)

    state.edit_field = ""
    # Show menu again for another edit
    return (
        f"✓ 已更新。\n\n{_render_edit_menu(existing)}",
        False,
    )


# ---------------------------------------------------------------------------
# Finalize first-time path
# ---------------------------------------------------------------------------


def _ensure_caller_identity(prof: _profile.OwnerProfile, state: WizardState) -> None:
    """v1.7.0: if the wizard caller's (platform, sender_id) isn't already in
    prof.identities, append it as an enabled entry. Idempotent — re-running
    /paid-setup never duplicates rows.

    This collapses the old setup_wizard.py:466 TODO (v1.6.x: have wizard
    auto-add the platform+sender_id of the invocation as the first
    identity). Without this, the owner has to hand-edit owner_profile.json
    to make even single-channel notifications work; with it, day-one ops
    work the moment the wizard finishes.
    """
    if not state.platform or not state.owner_id:
        return
    for ident in prof.identities:
        if (
            isinstance(ident, dict)
            and str(ident.get("platform", "")) == state.platform
            and str(ident.get("user_id", "")) == state.owner_id
        ):
            return  # already present — no-op
    prof.identities.append({
        "platform": state.platform,
        "user_id": state.owner_id,
        "home_chat_id": state.owner_id,
        "enabled": True,
        "name": prof.name or "",
    })


def _enabled_platforms(prof: _profile.OwnerProfile) -> list[str]:
    out: list[str] = []
    for ident in prof.identities:
        if not isinstance(ident, dict):
            continue
        if not ident.get("enabled", True):
            continue
        plat = str(ident.get("platform", "") or "")
        if plat and plat not in out:
            out.append(plat)
    return out


def _maybe_ask_preferred_platform(state: WizardState) -> tuple[str, bool]:
    """After Q5: peek at the would-be profile, including the auto-added
    caller identity, and decide whether to ask Q6.

    Skips Q6 when:
      - profile would have only 1 enabled channel (nothing to pick)
      - profile.preferred_platform already set (don't badger the owner)
    """
    existing = _profile.load_profile()
    if existing is None:
        # Build a throwaway preview profile so we can count post-add identities.
        preview = _profile.new_profile(
            owner_id=state.owner_id,
            name=state.answers.get("name", "") or "",
            voice_preset=state.answers.get("voice_preset", "founder"),
            preferred_language=state.answers.get("preferred_language", "auto"),
        )
    else:
        preview = existing
    _ensure_caller_identity(preview, state)
    enabled = _enabled_platforms(preview)

    already_set = bool((preview.preferred_platform or "").strip())
    if len(enabled) < 2 or already_set:
        return _finalize_first_time(state)

    state.awaiting_preferred_platform = True
    return (_render_preferred_platform_question(enabled), False)


def _render_preferred_platform_question(enabled_platforms: list[str]) -> str:
    """Q6 prompt — owner picks which channel PAID should DM by default."""
    lines = [
        "6/6 你有多个 IM channel — PAID 主要在哪里找你？",
        "（影响 approval card / discovery card / 告警 默认走哪条）",
        "",
    ]
    for i, plat in enumerate(enabled_platforms, start=1):
        lines.append(f"  {i}. {plat}")
    lines.append(f"  {len(enabled_platforms)+1}. auto — 让 PAID 挑（首条 enabled）")
    return "\n".join(lines)


def _consume_preferred_platform_answer(
    state: WizardState, answer: str
) -> tuple[str, bool]:
    """Parse Q6 answer + finalize. ``state.answers['preferred_platform']`` is
    set to the chosen platform string, or `_auto_` to leave empty."""
    existing = _profile.load_profile()
    if existing is None:
        preview = _profile.new_profile(
            owner_id=state.owner_id,
            name=state.answers.get("name", "") or "",
            voice_preset=state.answers.get("voice_preset", "founder"),
            preferred_language=state.answers.get("preferred_language", "auto"),
        )
    else:
        preview = existing
    _ensure_caller_identity(preview, state)
    enabled = _enabled_platforms(preview)

    raw = (answer or "").strip().lower()
    if not raw:
        return ("空回复 — 回平台名(lark/telegram/slack) 或数字。", False)

    auto_idx = str(len(enabled) + 1)
    if raw in ("auto", "skip", "自动", auto_idx):
        state.answers["preferred_platform"] = "_auto_"
        return _finalize_first_time(state)

    # numeric pick
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(enabled):
            state.answers["preferred_platform"] = enabled[n - 1]
            return _finalize_first_time(state)
        return (
            f"请回 1-{len(enabled)+1} 选择 channel,或 `auto` 让 PAID 自动挑。",
            False,
        )

    # name pick (case-insensitive; "feishu" maps to "lark" if user typed that)
    canonical = {"feishu": "lark", "lark": "lark", "telegram": "telegram",
                 "tg": "telegram", "slack": "slack"}
    picked = canonical.get(raw, raw)
    if picked in enabled:
        state.answers["preferred_platform"] = picked
        return _finalize_first_time(state)

    return (
        f"无法识别 {answer!r}。请回 1-{len(enabled)+1} 或平台名 "
        f"({', '.join(enabled)} / auto)。",
        False,
    )


def _finalize_first_time(state: WizardState) -> tuple[str, bool]:
    """Apply the 5 wizard answers and persist.

    v1.6.8: if an existing profile is loaded (e.g. after migration from v1.5
    or from a prior wizard run), apply answers as a PARTIAL update — never
    overwrite fields the wizard didn't ask about (e.g. topics.always_decline
    extracted from sop.md by the migration LLM). Prior to v1.6.8 we always
    built a fresh ``new_profile`` here, which silently wiped any field outside
    the 5-question scope.

    v1.7.0: auto-add caller identity to profile.identities if not present
    (collapses old TODO in this same function). If state.answers contains
    preferred_platform (Q6 was asked + answered), apply it.
    """
    existing = _profile.load_profile()
    if existing is not None:
        prof = existing
        # Apply only the answers explicitly given. Each branch is the SAME
        # logic as the edit-mode _apply_answer, so wizard first-time vs edit
        # stay symmetric.
        if "name" in state.answers:
            _apply_answer(prof, "name", state.answers["name"])
        if "voice_preset" in state.answers:
            _apply_answer(prof, "voice_preset", state.answers["voice_preset"])
        if "always_escalate" in state.answers:
            _apply_answer(prof, "always_escalate", state.answers["always_escalate"])
        if "preferred_language" in state.answers:
            _apply_answer(prof, "preferred_language", state.answers["preferred_language"])
        if "daily_cost_cap_usd" in state.answers:
            _apply_answer(prof, "daily_cost_cap_usd", state.answers["daily_cost_cap_usd"])
    else:
        # Truly first-time: no existing profile at all. Build fresh.
        voice_preset = state.answers.get("voice_preset", "founder")
        prof = _profile.new_profile(
            owner_id=state.owner_id,
            name=state.answers.get("name", "") or "",
            voice_preset=voice_preset,
            preferred_language=state.answers.get("preferred_language", "auto"),
        )
        escalate = state.answers.get("always_escalate")
        if isinstance(escalate, list) and escalate:
            prof.topics.always_escalate = escalate
        cost = state.answers.get("daily_cost_cap_usd")
        if isinstance(cost, (int, float)):
            prof.preferences.daily_cost_cap_usd = float(cost)
        elif cost == "_inf_":
            prof.preferences.daily_cost_cap_usd = 99999.0

    # v1.7.0: ensure the wizard caller's identity is on the profile.
    _ensure_caller_identity(prof, state)

    # v1.7.0: apply Q6 answer if present.
    pref = state.answers.get("preferred_platform")
    if pref == "_auto_":
        prof.preferred_platform = ""  # leave empty → fallback to identities[0]
    elif isinstance(pref, str) and pref:
        prof.preferred_platform = pref

    _profile.save_profile(prof)
    audit = _profile_sync.derive_from_profile(prof)

    with _LOCK:
        _WIZARD_STATE.pop((state.platform, state.owner_id), None)

    return (_render_complete_summary(prof, audit), True)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_edit_menu(prof: _profile.OwnerProfile) -> str:
    enabled = _enabled_platforms(prof)
    if len(enabled) >= 2:
        primary_line = (
            f"  • 主频道: {prof.preferred_platform or '(自动 — 用首条 enabled)'}\n"
        )
        menu_extra = "  6. 主频道  7. 保存退出"
    else:
        # Only 1 channel — no choice to make; hide the menu item to keep
        # the wizard small for single-channel owners.
        primary_line = ""
        menu_extra = "  6. 保存退出"
    return (
        f"PAID setup — 当前 profile:\n"
        f"  • 名字: {prof.name or '(未设置)'}\n"
        f"  • 语气: {prof.voice.tone}\n"
        f"  • Escalate topics: {', '.join(prof.topics.always_escalate) or '(空)'}\n"
        f"  • 默认语言: {prof.preferred_language}\n"
        f"  • 每日 LLM cost cap: ${prof.preferences.daily_cost_cap_usd:.2f}\n"
        f"{primary_line}\n"
        f"改哪个？回数字或字段名：\n"
        f"  1. 名字  2. 语气  3. 话题  4. 语言  5. 成本\n"
        f"{menu_extra}"
    )


def _render_complete_summary(prof: _profile.OwnerProfile, audit: dict) -> str:
    return (
        f"✓ PAID setup 完成！\n\n"
        f"  • 名字: {prof.name}\n"
        f"  • 语气: {prof.voice.tone}\n"
        f"  • Escalate topics: {', '.join(prof.topics.always_escalate)}\n"
        f"  • 默认语言: {prof.preferred_language}\n"
        f"  • 每日 LLM cost cap: ${prof.preferences.daily_cost_cap_usd:.2f}\n\n"
        f"已写文件: {', '.join(audit['wrote'])}\n\n"
        f"随时发 /paid-setup 修改任何字段。"
    )


# ---------------------------------------------------------------------------
# Answer parsing
# ---------------------------------------------------------------------------


def _parse_answer(field_key: str, raw: str) -> Any:
    """Parse owner's freeform answer into the correct type for ``field_key``.
    Returns the parsed value, or a string starting with ``"ERR:"`` followed
    by a human-readable error message (caller re-prompts)."""
    raw = raw.strip()
    if not raw:
        return f"ERR:空回复 — 请再发一次"

    if field_key == "name":
        if len(raw) > 80:
            return f"ERR:名字太长 (>80 字)"
        return raw

    if field_key == "voice_preset":
        picked = _PICK_MENUS["voice_preset"].get(raw.lower())
        if picked is None:
            return f"ERR:无法识别 {raw!r}，请回 1-4 或预设名"
        return picked

    if field_key == "always_escalate":
        if raw.lower() == "default":
            return "_default_"
        items = [s.strip() for s in raw.split(",") if s.strip()]
        if not items:
            return f"ERR:话题列表是空的 — 请逗号分隔，或回 `default`"
        return items

    if field_key == "preferred_language":
        picked = _PICK_MENUS["preferred_language"].get(raw.lower())
        if picked is None:
            return f"ERR:无法识别 {raw!r}，请回 1-4 或 auto/zh/en/ko"
        return picked

    if field_key == "daily_cost_cap_usd":
        if raw.lower() in ("inf", "infinity", "no", "none"):
            return "_inf_"
        try:
            v = float(raw.replace("$", "").replace(",", ""))
            if v < 0:
                return f"ERR:成本上限不能为负"
            if v > 10000:
                return f"ERR:看起来过大 (>{int(v)})，确认下数字？回 inf 表示不限"
            return v
        except ValueError:
            return f"ERR:无法解析 {raw!r} 为金额，请输入数字或 `inf`"

    return f"ERR:未知字段 {field_key}"


def _apply_answer(prof: _profile.OwnerProfile, field_key: str, value: Any) -> None:
    """Mutate ``prof`` to apply a single edit-mode answer."""
    if field_key == "name":
        prof.name = str(value)
    elif field_key == "voice_preset":
        from .profile import _VOICE_PRESETS  # type: ignore
        preset = _VOICE_PRESETS.get(value, _VOICE_PRESETS["founder"])
        prof.voice = _profile.Voice(
            tone=preset["tone"],
            style_notes=preset["style_notes"],
            do_not_say=list(preset["do_not_say"]),
            self_description=prof.voice.self_description,  # keep
        )
    elif field_key == "always_escalate":
        if value == "_default_":
            prof.topics.always_escalate = [
                "equity", "salary", "hiring", "customer", "finance"
            ]
        else:
            prof.topics.always_escalate = list(value)
    elif field_key == "preferred_language":
        prof.preferred_language = str(value)
    elif field_key == "daily_cost_cap_usd":
        if value == "_inf_":
            prof.preferences.daily_cost_cap_usd = 99999.0
        else:
            prof.preferences.daily_cost_cap_usd = float(value)


# ---------------------------------------------------------------------------
# Internal — TTL prune
# ---------------------------------------------------------------------------


def _prune_expired() -> None:
    cutoff = time.time() - _WIZARD_TTL_SEC
    with _LOCK:
        stale = [k for k, v in _WIZARD_STATE.items() if v.since_ts < cutoff]
        for k in stale:
            _WIZARD_STATE.pop(k, None)


def _clear_for_tests() -> None:  # pragma: no cover - test helper
    """Test-only: nuke all wizard state."""
    with _LOCK:
        _WIZARD_STATE.clear()
