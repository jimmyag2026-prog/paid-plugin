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
    mode: str = "first_time"  # "first_time" | "edit"
    step: int = 0              # 0 means just started; 1-5 means waiting for that question's answer
    answers: dict[str, Any] = field(default_factory=dict)
    edit_field: str = ""       # when mode="edit", which single field is being edited
    since_ts: float = 0.0


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


# ---------------------------------------------------------------------------
# First-time path consume
# ---------------------------------------------------------------------------


def _consume_first_time(state: WizardState, answer: str) -> tuple[str, bool]:
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

    # All 5 answered — build profile + derive + summary
    return _finalize_first_time(state)


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
        field_map = {
            "1": "name", "name": "name",
            "2": "voice_preset", "voice": "voice_preset", "tone": "voice_preset",
            "3": "always_escalate", "topics": "always_escalate", "escalate": "always_escalate",
            "4": "preferred_language", "language": "preferred_language", "lang": "preferred_language",
            "5": "daily_cost_cap_usd", "cost": "daily_cost_cap_usd", "cap": "daily_cost_cap_usd",
            "6": "done", "done": "done", "save": "done", "退出": "done",
        }
        picked = field_map.get(choice)
        if picked is None:
            return (
                "请回 1-6 选择字段（1=名字, 2=语气, 3=话题, 4=语言, 5=成本, 6=保存退出）",
                False,
            )
        if picked == "done":
            with _LOCK:
                _WIZARD_STATE.pop((state.platform, state.owner_id), None)
            return ("PAID setup 完成。", True)
        state.edit_field = picked
        qdef = next(q for q in _QUESTIONS if q["key"] == picked)
        return (qdef["prompt"], False)

    # Owner is responding to a specific field's question
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


def _finalize_first_time(state: WizardState) -> tuple[str, bool]:
    voice_preset = state.answers.get("voice_preset", "founder")
    prof = _profile.new_profile(
        owner_id=state.owner_id,
        name=state.answers.get("name", "") or "",
        voice_preset=voice_preset,
        preferred_language=state.answers.get("preferred_language", "auto"),
    )

    # always_escalate
    escalate = state.answers.get("always_escalate")
    if escalate == "_default_":
        # keep dataclass default (already 5 items)
        pass
    elif isinstance(escalate, list) and escalate:
        prof.topics.always_escalate = escalate

    # daily cost cap
    cost = state.answers.get("daily_cost_cap_usd")
    if isinstance(cost, (int, float)):
        prof.preferences.daily_cost_cap_usd = float(cost)
    elif cost == "_inf_":
        prof.preferences.daily_cost_cap_usd = 99999.0  # effectively no cap

    # Seed owner identity from wizard caller — caller passes platform; we
    # don't have user_id here without an additional event. Skip identities
    # at wizard time; owner adds them via `paid setup` CLI or hand-edits.
    # (TODO v1.6.x: have wizard auto-add the platform+sender_id of the
    # wizard invocation event as the first identity.)

    _profile.save_profile(prof)
    audit = _profile_sync.derive_from_profile(prof)

    with _LOCK:
        _WIZARD_STATE.pop((state.platform, state.owner_id), None)

    return (_render_complete_summary(prof, audit), True)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_edit_menu(prof: _profile.OwnerProfile) -> str:
    return (
        f"PAID setup — 当前 profile:\n"
        f"  • 名字: {prof.name or '(未设置)'}\n"
        f"  • 语气: {prof.voice.tone}\n"
        f"  • Escalate topics: {', '.join(prof.topics.always_escalate) or '(空)'}\n"
        f"  • 默认语言: {prof.preferred_language}\n"
        f"  • 每日 LLM cost cap: ${prof.preferences.daily_cost_cap_usd:.2f}\n\n"
        f"改哪个？回数字或字段名：\n"
        f"  1. 名字  2. 语气  3. 话题  4. 语言  5. 成本  6. 保存退出"
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
