"""Module Db — read-only web dashboard for PAID state.

A single-file Flask server that binds to 127.0.0.1 (loopback only) and
exposes a few read-only views over the PAID state directory. Designed for
the operator to glance at "what is PAID doing today" without grepping
JSONL files.

Bind to loopback by default — anyone with shell access already has the
data, so HTTP auth would be theatre. If the operator wants remote access
they can SSH-tunnel ``-L 7777:localhost:7777`` themselves; we never bind
0.0.0.0 by default.

Routes
------
``GET /``                      — top-of-funnel summary (today's metrics).
``GET /counterparties``        — list of cps sorted by last seen.
``GET /counterparties/<cp_id>`` — detail page for one cp.
``GET /pending``               — open approvals.
``GET /audit?date=YYYY-MM-DD`` — audit rows for a UTC date (default: today).
``GET /fatal``                 — last 50 fatal alerts verbatim.
``GET /api/summary.json``      — same data as ``/`` for tooling.

Usage
-----
::

    python3 -m paid dashboard --port 7777
    python3 -m paid dashboard --bind 0.0.0.0 --port 7777   # explicit opt-in

The dashboard is a thin wrapper over already-on-disk state. It runs without
the gateway running (great for post-mortem inspection).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import approval, identity, storage


# ---------------------------------------------------------------------------
# Data helpers (no Flask imports here so unit tests can poke the math).
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _read_all_audit() -> list[dict]:
    """v1.6.4: read audit entries from per-cp files + legacy (grace period)."""
    from . import audit as _audit
    return _audit.read_all_entries()


def _today_window() -> tuple[datetime, datetime]:
    today = datetime.now(timezone.utc).date()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _within(ts: str, start: datetime, end: datetime) -> bool:
    dt = _parse_iso(ts)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt < end


def _counterparty_health(last_seen: datetime | None) -> str:
    """Activity dot per design §2.4: 🟢 today / 🟡 this week / ⚪ older."""
    if last_seen is None:
        return "⚪"
    now = datetime.now(timezone.utc)
    age = now - last_seen
    if age <= timedelta(hours=24):
        return "🟢"
    if age <= timedelta(days=7):
        return "🟡"
    return "⚪"


def collect_summary() -> dict[str, Any]:
    """Today's headline metrics. Stable shape — also exposed at /api/summary.json."""
    audit = _read_all_audit()
    start, end = _today_window()
    today_rows = [r for r in audit if _within(r.get("ts", ""), start, end)]

    state_counts: Counter[str] = Counter()
    l1 = l4 = fb = 0
    for row in today_rows:
        action = row.get("action") or {}
        if isinstance(action, dict) and action.get("state"):
            state_counts[action["state"]] += 1
        extra = row.get("extra") or {}
        if extra.get("blocked_by") == "layer_1_prompt_injection":
            l1 += 1
        if extra.get("l4_ok") is False:
            l4 += 1
        if extra.get("fallback") is True:
            fb += 1

    total = sum(state_counts.values())
    direct_rate = (state_counts.get("direct", 0) / total * 100) if total else 0

    owner = identity.load_owner()

    # v1.5.5 A3: cost-cap snapshot for top-of-home progress bar. Read via
    # cost.cap_status — same source of truth as `bin/check_cost_cap.py`.
    try:
        from . import cost as _cost
        cost_status = _cost.cap_status()
    except Exception:
        cost_status = {
            "today_usd": 0.0, "daily_soft_cap": 5.0, "daily_hard_cap": 20.0,
            "weekly_soft_cap": 25.0, "enabled": False,
        }

    # v1.5.5 A7: platform_breakdown (schema only, no UI). Aggregates today's
    # decisions + total cp_count + pending count per platform. Surfaces in
    # /api/summary.json so external tools / future multi-pilot UI can read it.
    platform_breakdown = _compute_platform_breakdown(today_rows)

    return {
        "owner_name": (owner.name or owner.owner_id) if owner else None,
        "owner_identities": len(owner.identities) if owner else 0,
        "today_window_start": start.isoformat(),
        "today_window_end": end.isoformat(),
        "total_decisions_today": total,
        "direct_today": state_counts.get("direct", 0),
        "request_today": state_counts.get("request", 0),
        "decline_today": state_counts.get("decline", 0),
        "direct_rate_today": round(direct_rate, 1),
        "direct_rate_target": 50,  # master design §6
        "l1_hits_today": l1,
        "l4_hits_today": l4,
        "classifier_fallback_today": fb,
        "pending_count": len(approval.list_pending()),
        "audit_rows_total": len(audit),
        # v1.5.5 A3 cost fields
        "cost_today_usd": round(float(cost_status.get("today_usd", 0.0)), 4),
        "cost_soft_cap_usd": float(cost_status.get("daily_soft_cap", 0.0)),
        "cost_hard_cap_usd": float(cost_status.get("daily_hard_cap", 0.0)),
        "cost_cap_enabled": bool(cost_status.get("enabled", False)),
        # v1.5.5 A7 platform breakdown (schema only, no UI yet)
        "platform_breakdown": platform_breakdown,
    }


def _compute_platform_breakdown(today_audit_rows: list[dict]) -> dict[str, dict[str, int]]:
    """v1.5.5 A7: per-platform aggregate (cp_count + decisions_today + pending_count).

    Iterates owner identities + all counterparties to learn the platform set,
    then counts:
      - cp_count: cps registered on that platform (any role except "ignored"/"blocked")
      - decisions_today: audit rows whose row.platform (or derived cp.platform)
        match. We tolerate older audit rows that lack the platform field by
        falling back to a cp_id → platform lookup.
      - pending_count: pending approvals for cps on that platform.

    Output shape is platform → {cp_count, decisions_today, pending_count, active_role_counts}.
    Empty for platforms with no counterparties.
    """
    cps = identity.list_all_counterparties()
    # cp_id → platform lookup for older audit-row fallback
    cpid_to_plat = {c.cp_id: c.platform for c in cps}

    out: dict[str, dict[str, Any]] = {}

    def _slot(p: str) -> dict[str, Any]:
        if p not in out:
            out[p] = {"cp_count": 0, "decisions_today": 0,
                      "pending_count": 0,
                      "role_counts": {}}
        return out[p]

    # v1.5.6 review fix #7: cp_count intentionally counts ALL roles
    # including 'ignored' and 'blocked'. role_counts gives the breakdown so
    # callers can filter if they want active-only. Surfacing the total
    # platform footprint (incl. silenced cps) is the more honest signal for
    # a future multi-pilot dashboard view.
    for c in cps:
        if not c.platform:
            continue
        slot = _slot(c.platform)
        slot["cp_count"] += 1
        slot["role_counts"][c.role] = slot["role_counts"].get(c.role, 0) + 1

    # Today's decisions
    for r in today_audit_rows:
        plat = r.get("platform") or ""
        if not plat:
            cp_id = r.get("counterparty") or ""
            plat = cpid_to_plat.get(cp_id, "")
        if plat:
            _slot(plat)["decisions_today"] += 1

    # Pending approvals (J3)
    try:
        for req in approval.list_pending():
            plat = req.counterparty_platform or ""
            if plat:
                _slot(plat)["pending_count"] += 1
    except Exception:
        pass

    return out


def _safe_truncate(text: str | None, n: int) -> str:
    """Truncate to n chars without slicing a multi-byte char (CJK safe).

    Python str slicing is by code point, not byte — CJK is naturally safe
    when ``text`` is already a unicode str. We collapse newlines / extra
    whitespace so the truncated preview is one line. ``None`` is accepted
    as a courtesy (v1.5.6 review fix #5 — type hint widened to match the
    implementation) and returns ``""``.
    """
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= n:
        return flat
    return flat[: max(0, n - 1)] + "…"


def _derive_review_next_action(stage: str, last_event_kind: str) -> str:
    """Owner-facing one-liner: who's PAID waiting on?

    Mapping is intentionally coarse — anything more detailed should live
    in the review session itself, not the dashboard.
    """
    s = (stage or "").upper()
    if s == "CLOSED":
        return "closed"
    if s == "INTAKE":
        return "awaiting subject from junior"
    if s == "SUBJECT":
        return "scanning material"
    if s == "SCAN":
        return "preparing Q&A for junior"
    if s == "QA":
        return "awaiting junior reply"
    if s == "MERGE":
        return "merging findings"
    if s == "GATE":
        return "awaiting your gate decision"
    return s.lower() or "unknown"


def _scan_review_sessions_dir(root: Path, archived: bool) -> list[dict[str, Any]]:
    """Internal: yield rows from one sessions root (active or archived)."""
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    iter_dirs: list[Path] = []
    if archived:
        # _closed/<month>/<sid>/
        for month_dir in root.iterdir():
            if month_dir.is_dir():
                iter_dirs.extend(p for p in month_dir.iterdir() if p.is_dir())
    else:
        # sessions/<sid>/ — but skip _closed which is for archives
        for p in root.iterdir():
            if p.is_dir() and p.name != "_closed":
                iter_dirs.append(p)
    for sid_dir in iter_dirs:
        meta = sid_dir / "meta.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        stage = str(data.get("stage", "") or "")
        rows.append({
            "sid": sid_dir.name,
            "cp_id": data.get("cp_id", ""),
            "platform": data.get("platform", ""),
            "stage": stage,
            "verdict": data.get("verdict", ""),
            "rounds": int(data.get("rounds", 0) or 0),
            "max_rounds": int(data.get("max_rounds", 3) or 3),
            "created_at": data.get("created_at", ""),
            "updated_at": data.get("updated_at", ""),
            "closed_at": data.get("closed_at"),
            "delivery_failed": bool(data.get("delivery_failed", False)),
            "is_archived": archived,
            "is_closed": stage == "CLOSED" or archived,
            "next_action": _derive_review_next_action(
                stage, data.get("last_event_kind", "")
            ),
        })
    return rows


def collect_review_sessions(
    include_closed: bool = False, limit: int | None = None
) -> list[dict[str, Any]]:
    """v1.5.5 A4: enumerate review skill sessions.

    Default returns only ACTIVE sessions (stage != CLOSED, not archived).
    Pass ``include_closed=True`` to also include archived sessions under
    ``review/sessions/_closed/<month>/``. Rows are sorted newest-updated
    first. A bad/missing/corrupt ``meta.json`` is skipped silently.
    """
    sessions_root = storage.PAID_DIR / "review" / "sessions"
    rows = _scan_review_sessions_dir(sessions_root, archived=False)
    if include_closed:
        rows += _scan_review_sessions_dir(
            sessions_root / "_closed", archived=True,
        )
    rows.sort(
        key=lambda r: r.get("updated_at") or r.get("created_at") or "",
        reverse=True,
    )
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def collect_trend(days: int = 7) -> dict[str, Any]:
    """v1.5.5 A5: aggregate per UTC-day series for the last `days` days.

    Output:
        {
            "days": int,
            "labels":  ["YYYY-MM-DD", ...],   # ordered oldest → newest
            "direct":  [int, ...],
            "request": [int, ...],
            "decline": [int, ...],
            "cost_usd": [float, ...],
            "totals": {"direct": int, ..., "cost_usd": float},
        }

    Days with no data render as 0 (so Chart.js gets a continuous series).
    Robust against bad rows in audit_log / cost_ledger (skip silently).
    """
    days = max(1, int(days))
    today_utc = datetime.now(timezone.utc).date()

    # Pre-build the date axis oldest → newest
    date_axis = [today_utc - timedelta(days=i) for i in range(days - 1, -1, -1)]
    date_to_idx = {d.isoformat(): i for i, d in enumerate(date_axis)}

    direct = [0] * days
    request = [0] * days
    decline = [0] * days
    cost_usd = [0.0] * days

    # Audit: bucket by UTC date of row.ts
    audit_rows = _read_all_audit()
    for r in audit_rows:
        ts = r.get("ts", "")
        try:
            row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        day_key = row_dt.astimezone(timezone.utc).date().isoformat()
        idx = date_to_idx.get(day_key)
        if idx is None:
            continue
        act = r.get("action") or {}
        if not isinstance(act, dict):
            continue
        state = act.get("state")
        if state == "direct":
            direct[idx] += 1
        elif state == "request":
            request[idx] += 1
        elif state == "decline":
            decline[idx] += 1

    # Cost ledger: prefer entry["date"] (UTC date string already there)
    ledger_rows = _read_jsonl(storage.PAID_DIR / "cost_ledger.jsonl")
    for r in ledger_rows:
        day_key = r.get("date") or ""
        if not day_key:
            # Fallback to ts
            ts = r.get("ts", "")
            try:
                day_key = datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).astimezone(timezone.utc).date().isoformat()
            except Exception:
                continue
        idx = date_to_idx.get(day_key)
        if idx is None:
            continue
        try:
            cost_usd[idx] += float(r.get("cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue

    cost_usd = [round(v, 4) for v in cost_usd]

    return {
        "days": days,
        "labels": [d.isoformat() for d in date_axis],
        "direct": direct,
        "request": request,
        "decline": decline,
        "cost_usd": cost_usd,
        "totals": {
            "direct": sum(direct),
            "request": sum(request),
            "decline": sum(decline),
            "cost_usd": round(sum(cost_usd), 4),
        },
    }


def collect_recent_activity(limit: int = 10) -> list[dict[str, Any]]:
    """Newest N decisions (v1.5.5 A3): for the home page's quick-glance table.

    Returns rows with ts/cp/state/topic/q_preview (80 chars). Empty list
    when audit log missing.
    """
    audit = _read_all_audit()
    audit.sort(key=lambda r: r.get("ts", ""), reverse=True)
    out: list[dict[str, Any]] = []
    for r in audit[: max(0, limit)]:
        act = r.get("action") or {}
        cls = r.get("classification") or {}
        out.append({
            "ts": r.get("ts", ""),
            "cp": r.get("counterparty", ""),
            "state": act.get("state", "—") if isinstance(act, dict) else "—",
            "topic": cls.get("topic", "") if isinstance(cls, dict) else "",
            "q_preview": _safe_truncate(r.get("junior_msg", "") or "", 80),
        })
    return out


def collect_counterparties() -> list[dict[str, Any]]:
    """All cps with last-seen + activity-dot, newest first."""
    audit = _read_all_audit()
    last_seen: dict[str, str] = {}
    msg_count: Counter[str] = Counter()
    for row in audit:
        cp = row.get("counterparty")
        ts = row.get("ts", "")
        if not cp:
            continue
        if ts > last_seen.get(cp, ""):
            last_seen[cp] = ts
        msg_count[cp] += 1

    rows: list[dict[str, Any]] = []
    for cp in identity.list_all_counterparties():
        ts = last_seen.get(cp.cp_id, "")
        dt = _parse_iso(ts)
        rows.append(
            {
                "cp_id": cp.cp_id,
                "platform": cp.platform,
                "user_id": cp.user_id,
                "display_name": cp.display_name or "(none)",
                "role": cp.role,
                "topics_allowed": cp.topics_allowed,
                "topics_always_escalate": cp.topics_always_escalate,
                "ignore_reason": cp.ignore_reason,
                "ignore_set_at": cp.ignore_set_at,
                "last_seen": ts,
                "msg_count": msg_count.get(cp.cp_id, 0),
                "health": _counterparty_health(dt),
            }
        )
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    return rows


def collect_audit_for_date(date_str: str | None) -> tuple[str, list[dict]]:
    """Return (date used, rows in that UTC day)."""
    today = datetime.now(timezone.utc).date()
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            d = today
    else:
        d = today
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    audit = _read_all_audit()
    rows = [r for r in audit if _within(r.get("ts", ""), start, end)]
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return d.isoformat(), rows


# ---------------------------------------------------------------------------
# Flask app — built on demand so tests can import this module without flask.
# ---------------------------------------------------------------------------


def build_app():
    """Construct the Flask app. ``flask`` is imported here, not at top level,
    so the rest of paid (and pytest) doesn't pay the import cost or require
    flask just to read paid.dashboard."""
    try:
        from flask import Flask, abort, jsonify, render_template_string, request
    except ImportError as exc:  # pragma: no cover — surfaced at runtime
        raise SystemExit(
            "PAID dashboard needs Flask. Install with:\n"
            "    pip install --user flask\n"
            f"(import error: {exc})"
        ) from exc

    # v1.5.6 (review fix #2): serve vendored Chart.js from paid/static/ so
    # the dashboard works offline / behind corp firewalls and removes the
    # CDN supply-chain surface ahead of the planned Caddy reverse-proxy.
    _static_dir = str(Path(__file__).resolve().parent / "static")
    app = Flask("paid-dashboard", static_folder=_static_dir,
                static_url_path="/static")
    app.jinja_env.autoescape = True

    @app.route("/")
    def home():
        s = collect_summary()
        cps = collect_counterparties()
        pending = [
            {
                "request_id": r.request_id,
                "cp": r.counterparty_display or r.counterparty_user_id,
                "platform": r.counterparty_platform,
                "topic": r.topic,
                "stakes": r.stakes,
                "confidence": r.confidence,
                "age_min": int((datetime.now(timezone.utc).timestamp() - r.ts_created) / 60),
                "q_preview": _safe_truncate(r.junior_question, 80),
            }
            for r in approval.list_pending()
        ]
        recent = collect_recent_activity(limit=10)
        reviews_active = collect_review_sessions(include_closed=False, limit=10)
        trend7 = collect_trend(days=7)
        from . import metrics_progress as _mp
        mp_rows = _mp.collect()
        mp_done = _mp.n_done(mp_rows)
        return render_template_string(
            _HOME_TEMPLATE, s=s, cps=cps[:10], pending=pending, recent=recent,
            reviews_active=reviews_active, trend7=trend7,
            mp_done=mp_done, mp_total=len(mp_rows),
        )

    @app.route("/counterparties")
    def counterparties_view():
        cps = collect_counterparties()
        return render_template_string(_CPS_TEMPLATE, cps=cps)

    @app.route("/counterparties/<cp_id>")
    def counterparty_detail(cp_id: str):
        # Find the cp record + recent audit messages.
        cp = next((c for c in identity.list_all_counterparties() if c.cp_id == cp_id), None)
        if cp is None:
            abort(404)
        audit = _read_all_audit()
        rows = [r for r in audit if r.get("counterparty") == cp_id]
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        # v1.5.5 A4: review history for this cp (active + closed)
        all_reviews = collect_review_sessions(include_closed=True)
        cp_reviews = [r for r in all_reviews if r["cp_id"] == cp_id]
        return render_template_string(
            _CP_DETAIL_TEMPLATE, cp=cp, rows=rows[:50], reviews=cp_reviews,
        )

    @app.route("/metrics-progress")
    def metrics_progress_view():
        from . import metrics_progress as _mp
        rows = _mp.collect()
        return render_template_string(
            _METRICS_PROGRESS_TEMPLATE,
            rows=rows, n_done=_mp.n_done(rows), n_total=len(rows),
        )

    @app.route("/trends")
    def trends_view():
        t7 = collect_trend(days=7)
        t30 = collect_trend(days=30)
        return render_template_string(_TRENDS_TEMPLATE, t7=t7, t30=t30)

    @app.route("/api/trends.json")
    def api_trends():
        return jsonify({
            "7": collect_trend(days=7),
            "30": collect_trend(days=30),
        })

    @app.route("/reviews")
    def reviews_view():
        active = collect_review_sessions(include_closed=False)
        closed = [r for r in collect_review_sessions(include_closed=True) if r["is_closed"]]
        # Keep closed list bounded — recent 50 only
        closed = closed[:50]
        return render_template_string(
            _REVIEWS_TEMPLATE, active=active, closed=closed,
        )

    @app.route("/pending")
    def pending_view():
        pendings = approval.list_pending()
        return render_template_string(_PENDING_TEMPLATE, pendings=pendings)

    @app.route("/audit")
    def audit_view():
        date_str = request.args.get("date")
        used_date, rows = collect_audit_for_date(date_str)
        return render_template_string(
            _AUDIT_TEMPLATE, used_date=used_date, rows=rows[:200]
        )

    @app.route("/fatal")
    def fatal_view():
        rows = _read_jsonl(storage.PAID_DIR / "fatal_alerts.jsonl")
        rows.reverse()
        return render_template_string(_FATAL_TEMPLATE, rows=rows[:50])

    @app.route("/api/summary.json")
    def api_summary():
        return jsonify(collect_summary())

    return app


# ---------------------------------------------------------------------------
# Templates — single-file Jinja strings. We deliberately avoid pulling
# in a templates/ directory so the plugin stays one folder.
# ---------------------------------------------------------------------------

_BASE_HEAD = """\
<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <title>{{ title or "PAID dashboard" }}</title>
  <style>
    body { font: 14px/1.5 -apple-system, "SF Pro Text", system-ui, sans-serif;
           margin: 0; padding: 24px; max-width: 1100px; color: #222;
           background: #fafafa; }
    h1 { font-size: 22px; margin: 0 0 8px; }
    h2 { font-size: 16px; margin: 28px 0 8px; color: #444; }
    nav { margin: 0 0 18px; padding: 8px 12px; background: #fff;
          border: 1px solid #e3e3e3; border-radius: 6px; }
    nav a { margin-right: 14px; color: #2266cc; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 18px; }
    .card { background: #fff; padding: 12px 14px; border: 1px solid #e3e3e3;
            border-radius: 6px; }
    .card .lbl { font-size: 11px; color: #888; text-transform: uppercase;
                 letter-spacing: 0.5px; }
    .card .v { font-size: 22px; font-weight: 600; margin-top: 4px; }
    .ok { color: #1b8a3a; }
    .warn { color: #b85c00; }
    .bad { color: #c33; }
    table { border-collapse: collapse; width: 100%; background: #fff;
            border: 1px solid #e3e3e3; border-radius: 6px; overflow: hidden; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #f0f0f0;
             vertical-align: top; }
    th { background: #f7f7f7; font-weight: 600; font-size: 12px; color: #555; }
    code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 12px; }
    .pill { display: inline-block; padding: 1px 8px; border-radius: 10px;
            font-size: 11px; background: #eef; color: #335; }
    .stamp { color: #999; font-size: 12px; }
    /* v1.5.5 A3 — top-of-home progress bars */
    .barwrap { background: #fff; padding: 10px 14px; border: 1px solid #e3e3e3;
               border-radius: 6px; margin-bottom: 10px; }
    .barwrap .label { display: flex; justify-content: space-between;
                      font-size: 12px; color: #555; margin-bottom: 4px; }
    .barwrap .label b { color: #222; font-weight: 600; }
    .bar { width: 100%; height: 10px; background: #eee; border-radius: 5px;
           overflow: hidden; position: relative; }
    .bar .fill { height: 100%; transition: width 200ms ease; }
    .bar .fill.ok   { background: #1b8a3a; }
    .bar .fill.warn { background: #e0a020; }
    .bar .fill.bad  { background: #c33; }
    .bar .marker { position: absolute; top: -2px; width: 2px; height: 14px;
                   background: #888; }
    .muted { color: #888; font-size: 11px; margin-top: 4px; }
    /* v1.5.5 A5 — chart containers */
    .chartwrap { background: #fff; padding: 12px 14px; border: 1px solid #e3e3e3;
                 border-radius: 6px; margin-bottom: 14px; }
    .chartwrap h3 { font-size: 14px; margin: 0 0 8px; color: #444; font-weight: 600; }
    .chart-fallback { color: #c33; padding: 12px; font-size: 12px;
                      background: #fdf6f6; border-radius: 4px; }
    canvas.chart { max-width: 100%; height: 180px !important; }
    canvas.chart-lg { max-width: 100%; height: 280px !important; }
  </style>
  <!-- v1.5.6 A5: Chart.js v4 vendored at paid/static/chart.umd.min.js.
       Was loaded from jsdelivr CDN in v1.5.5 but vendored in v1.5.6 to
       (1) work offline / behind firewalls, (2) remove CDN supply-chain
       surface ahead of Caddy reverse-proxy deployment. If the file is
       missing for any reason the inline JS fallback below kicks in. -->
  <script src="/static/chart.umd.min.js"></script>
</head><body>
<nav>
  <a href="/">Summary</a>
  <a href="/counterparties">Counterparties</a>
  <a href="/pending">Pending</a>
  <a href="/reviews">Reviews</a>
  <a href="/trends">Trends</a>
  <a href="/metrics-progress">Progress</a>
  <a href="/audit">Audit</a>
  <a href="/fatal">Fatal alerts</a>
  <a href="/api/summary.json">JSON</a>
</nav>
"""

_HOME_TEMPLATE = _BASE_HEAD + """\
<h1>PAID — Today</h1>
<p class="stamp">{{ s.today_window_start }} → {{ s.today_window_end }}
  · owner: {% if s.owner_name %}<b>{{ s.owner_name }}</b> ({{ s.owner_identities }} identities){% else %}<span class="bad">NOT CONFIGURED</span>{% endif %}</p>

<div class="grid">
  <div class="card"><div class="lbl">Decisions today</div>
    <div class="v">{{ s.total_decisions_today }}</div></div>
  <div class="card"><div class="lbl">Direct-rate</div>
    <div class="v {% if s.direct_rate_today >= 50 %}ok{% else %}bad{% endif %}">{{ s.direct_rate_today }}%</div></div>
  <div class="card"><div class="lbl">Pending approvals</div>
    <div class="v {% if s.pending_count > 0 %}warn{% endif %}">{{ s.pending_count }}</div></div>
  <div class="card"><div class="lbl">L1 / L4 / fallback</div>
    <div class="v">{{ s.l1_hits_today }} / {{ s.l4_hits_today }} / {{ s.classifier_fallback_today }}</div></div>
</div>

{# v1.5.5 A3 — direct-rate progress bar (vs 50% target) #}
{% set dr_pct = s.direct_rate_today %}
{% set dr_klass = "ok" if dr_pct >= s.direct_rate_target else ("warn" if dr_pct >= 30 else "bad") %}
<div class="barwrap">
  <div class="label">
    <span>Direct-answer rate today</span>
    <span><b>{{ "%.1f" | format(dr_pct) }}%</b> &nbsp;/&nbsp; target {{ s.direct_rate_target }}%</span>
  </div>
  <div class="bar">
    <div class="fill {{ dr_klass }}" style="width: {{ [dr_pct, 100]|min }}%"></div>
    <div class="marker" style="left: {{ s.direct_rate_target }}%"></div>
  </div>
  <div class="muted">{{ s.direct_today }} direct · {{ s.request_today }} request · {{ s.decline_today }} decline</div>
</div>

{# v1.5.5 A3 — cost-today bar (vs soft/hard caps) #}
{% if s.cost_cap_enabled %}
{% set hard = s.cost_hard_cap_usd or 1.0 %}
{% set pct = (s.cost_today_usd / hard * 100) if hard > 0 else 0 %}
{% set pct_clamped = [pct, 100]|min %}
{% set marker_soft_pct = (s.cost_soft_cap_usd / hard * 100) if hard > 0 else 0 %}
{% set cost_klass = "bad" if pct >= 100 else ("warn" if s.cost_today_usd >= s.cost_soft_cap_usd else "ok") %}
<div class="barwrap">
  <div class="label">
    <span>LLM cost today</span>
    <span><b>${{ "%.2f" | format(s.cost_today_usd) }}</b>
      &nbsp;/&nbsp; soft ${{ "%.2f" | format(s.cost_soft_cap_usd) }}
      &nbsp;/&nbsp; hard ${{ "%.2f" | format(s.cost_hard_cap_usd) }}</span>
  </div>
  <div class="bar">
    <div class="fill {{ cost_klass }}" style="width: {{ pct_clamped }}%"></div>
    <div class="marker" style="left: {{ [marker_soft_pct, 100]|min }}%"></div>
  </div>
  <div class="muted">Soft-cap marker shown on bar; bar fills to {{ "%.0f" | format(pct_clamped) }}% of hard cap.</div>
</div>
{% else %}
<div class="barwrap"><div class="label"><span>LLM cost today</span>
  <span class="muted">cap disabled — see settings.cost.enabled</span></div></div>
{% endif %}

{# v1.5.5 A6 — six-indicator progress mini-badge #}
<div class="barwrap" style="display:flex; justify-content:space-between; align-items:center;">
  <div>
    <span style="font-size: 13px; color: #555;">Master-design §6 progress</span>
    &nbsp;<b>{{ mp_done }}/{{ mp_total }}</b> indicators done
  </div>
  <a class="stamp" href="/metrics-progress">details →</a>
</div>

{# v1.5.5 A5 — compact 7-day trends on home #}
<div class="chartwrap">
  <h3>7-day trends <a class="stamp" href="/trends" style="font-weight: normal; margin-left: 8px;">view 30-day →</a></h3>
  <canvas id="homeTrend7" class="chart"></canvas>
  <noscript><div class="chart-fallback">Charts require JavaScript. See <a href="/api/trends.json">raw JSON</a>.</div></noscript>
  <div id="homeTrendFallback" class="chart-fallback" style="display:none">
    Chart library failed to load — see <a href="/api/trends.json">/api/trends.json</a> for raw data.
  </div>
</div>
<script>
(function() {
  if (typeof Chart === "undefined") {
    document.getElementById("homeTrendFallback").style.display = "block";
    return;
  }
  var ctx = document.getElementById("homeTrend7");
  new Chart(ctx, {
    type: "line",
    data: {
      labels: {{ trend7.labels | tojson }},
      datasets: [
        {label: "direct",  data: {{ trend7.direct  | tojson }}, borderColor: "#1b8a3a", backgroundColor: "rgba(27,138,58,0.1)", tension: 0.2, fill: false},
        {label: "request", data: {{ trend7.request | tojson }}, borderColor: "#e0a020", backgroundColor: "rgba(224,160,32,0.1)", tension: 0.2, fill: false},
        {label: "decline", data: {{ trend7.decline | tojson }}, borderColor: "#c33",    backgroundColor: "rgba(204,51,51,0.1)",  tension: 0.2, fill: false}
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {legend: {position: "bottom", labels: {boxWidth: 12, font: {size: 11}}}},
      scales: {y: {beginAtZero: true, ticks: {precision: 0}}}
    }
  });
})();
</script>

<h2>Pending approvals</h2>
{% if not pending %}<p>(none)</p>{% else %}
<table><tr><th>id</th><th>from</th><th>topic</th><th>stakes</th><th>conf</th><th>age</th><th>q</th></tr>
{% for p in pending %}<tr>
  <td><code>#{{ p.request_id }}</code></td>
  <td>{{ p.cp }} <span class="pill">{{ p.platform }}</span></td>
  <td>{{ p.topic }}</td><td>{{ p.stakes }}</td>
  <td>{{ "%.2f" | format(p.confidence) }}</td>
  <td>{{ p.age_min }}m</td>
  <td>{{ p.q_preview }}</td>
</tr>{% endfor %}</table>
{% endif %}

<h2>Counterparties (top 10 by last seen)</h2>
{% if not cps %}<p>(none)</p>{% else %}
<table><tr><th></th><th>display</th><th>platform</th><th>role</th><th>msgs</th><th>last seen</th></tr>
{% for c in cps %}<tr>
  <td>{{ c.health }}</td>
  <td><a href="/counterparties/{{ c.cp_id }}">{{ c.display_name }}</a></td>
  <td>{{ c.platform }}</td>
  <td>{{ c.role }}</td>
  <td>{{ c.msg_count }}</td>
  <td class="stamp">{{ c.last_seen[:19] or "—" }}</td>
</tr>{% endfor %}</table>
{% endif %}

{# v1.5.5 A4 — active review sessions summary #}
<h2>Active review sessions ({{ reviews_active|length }})
  <a class="stamp" href="/reviews" style="font-weight: normal; margin-left: 8px;">view all →</a></h2>
{% if not reviews_active %}<p>(none)</p>{% else %}
<table><tr><th>sid</th><th>cp</th><th>platform</th><th>stage</th><th>rounds</th><th>updated</th><th>next action</th></tr>
{% for r in reviews_active %}<tr>
  <td><code>{{ r.sid[:12] }}</code></td>
  <td>{{ r.cp_id or "—" }}</td>
  <td><span class="pill">{{ r.platform or "—" }}</span></td>
  <td>{{ r.stage }}</td>
  <td>{{ r.rounds }}/{{ r.max_rounds }}</td>
  <td class="stamp">{{ (r.updated_at or "")[:19] }}</td>
  <td>{{ r.next_action }}</td>
</tr>{% endfor %}</table>
{% endif %}

{# v1.5.5 A3 — recent activity with question preview (80 chars) #}
<h2>Recent activity (newest {{ recent|length }})</h2>
{% if not recent %}<p>(no audit rows yet)</p>{% else %}
<table><tr><th>ts</th><th>cp</th><th>state</th><th>topic</th><th>q</th></tr>
{% for r in recent %}<tr>
  <td class="stamp">{{ r.ts[:19] }}</td>
  <td>{{ r.cp or "—" }}</td>
  <td>{{ r.state }}</td>
  <td>{{ r.topic or "—" }}</td>
  <td>{{ r.q_preview }}</td>
</tr>{% endfor %}</table>
{% endif %}
</body></html>
"""

_CPS_TEMPLATE = _BASE_HEAD + """\
<h1>Counterparties ({{ cps|length }})</h1>
<table><tr><th></th><th>display</th><th>platform</th><th>role</th><th>topics_allowed</th>
  <th>msgs</th><th>last seen</th><th>ignore reason</th></tr>
{% for c in cps %}<tr>
  <td>{{ c.health }}</td>
  <td><a href="/counterparties/{{ c.cp_id }}">{{ c.display_name }}</a></td>
  <td>{{ c.platform }}</td>
  <td>{{ c.role }}</td>
  <td>{{ c.topics_allowed | join(", ") or "—" }}</td>
  <td>{{ c.msg_count }}</td>
  <td class="stamp">{{ c.last_seen[:19] or "—" }}</td>
  <td>{{ c.ignore_reason or "—" }}{% if c.ignore_set_at %}<br><span class="stamp">{{ c.ignore_set_at }}</span>{% endif %}</td>
</tr>{% endfor %}</table>
</body></html>
"""

_CP_DETAIL_TEMPLATE = _BASE_HEAD + """\
<h1>{{ cp.display_name or cp.cp_id }}</h1>
<p>
  <span class="pill">{{ cp.platform }}</span>
  <code>{{ cp.user_id }}</code> · role <b>{{ cp.role }}</b>
</p>
<p>topics_allowed: {{ cp.topics_allowed | join(", ") or "—" }}<br>
   topics_always_escalate: {{ cp.topics_always_escalate | join(", ") or "—" }}</p>
{% if cp.ignore_reason %}
<p class="bad">Ignored at {{ cp.ignore_set_at }} — {{ cp.ignore_reason }}</p>
{% endif %}
{% if cp.notes %}<p><i>{{ cp.notes }}</i></p>{% endif %}

{# v1.5.5 A4 — cp's review history (active + archived) #}
{% if reviews %}
<h2>Review sessions for this cp ({{ reviews|length }})</h2>
<table><tr><th>sid</th><th>stage</th><th>verdict</th><th>rounds</th><th>created</th><th>closed</th></tr>
{% for r in reviews %}<tr>
  <td><code>{{ r.sid[:12] }}</code></td>
  <td>{{ r.stage }}</td>
  <td>{{ r.verdict or "—" }}</td>
  <td>{{ r.rounds }}/{{ r.max_rounds }}</td>
  <td class="stamp">{{ (r.created_at or "")[:19] }}</td>
  <td class="stamp">{{ (r.closed_at or "")[:19] or "—" }}</td>
</tr>{% endfor %}</table>
{% endif %}

<h2>Recent activity (newest 50)</h2>
<table><tr><th>ts</th><th>state</th><th>topic</th><th>conf</th><th>q</th></tr>
{% for r in rows %}
{% set act = r.action or {} %}
{% set cls = r.classification or {} %}
<tr>
  <td class="stamp">{{ r.ts[:19] }}</td>
  <td>{{ act.state or "—" }}</td>
  <td>{{ cls.topic or "—" }}</td>
  <td>{{ "%.2f" | format(cls.confidence|default(0)) }}</td>
  <td>{{ (r.junior_msg or "")[:80] }}</td>
</tr>{% endfor %}</table>
</body></html>
"""

_REVIEWS_TEMPLATE = _BASE_HEAD + """\
<h1>Review sessions</h1>

<h2>Active ({{ active|length }})</h2>
{% if not active %}<p>(none)</p>{% else %}
<table><tr><th>sid</th><th>cp</th><th>platform</th><th>stage</th><th>verdict</th><th>rounds</th>
  <th>created</th><th>updated</th><th>next action</th></tr>
{% for r in active %}<tr>
  <td><code>{{ r.sid[:12] }}</code></td>
  <td>{% if r.cp_id %}<a href="/counterparties/{{ r.cp_id }}">{{ r.cp_id }}</a>{% else %}—{% endif %}</td>
  <td><span class="pill">{{ r.platform or "—" }}</span></td>
  <td>{{ r.stage }}</td>
  <td>{{ r.verdict or "—" }}</td>
  <td>{{ r.rounds }}/{{ r.max_rounds }}</td>
  <td class="stamp">{{ (r.created_at or "")[:19] }}</td>
  <td class="stamp">{{ (r.updated_at or "")[:19] }}</td>
  <td>{{ r.next_action }}</td>
</tr>{% endfor %}</table>
{% endif %}

<h2>Closed (recent {{ closed|length }})</h2>
{% if not closed %}<p>(none)</p>{% else %}
<table><tr><th>sid</th><th>cp</th><th>verdict</th><th>rounds</th><th>created</th><th>closed</th></tr>
{% for r in closed %}<tr>
  <td><code>{{ r.sid[:12] }}</code></td>
  <td>{% if r.cp_id %}<a href="/counterparties/{{ r.cp_id }}">{{ r.cp_id }}</a>{% else %}—{% endif %}</td>
  <td>{{ r.verdict or "—" }}</td>
  <td>{{ r.rounds }}/{{ r.max_rounds }}</td>
  <td class="stamp">{{ (r.created_at or "")[:19] }}</td>
  <td class="stamp">{{ (r.closed_at or "")[:19] or "—" }}</td>
</tr>{% endfor %}</table>
{% endif %}
</body></html>
"""

_METRICS_PROGRESS_TEMPLATE = _BASE_HEAD + """\
<h1>Master-design §6 — progress ({{ n_done }}/{{ n_total }})</h1>
<p class="stamp">Six hard indicators. ✓ = done. Manual rows toggle via <code>settings.metrics_progress.*</code>; derived rows auto-update from PAID state.</p>

<div class="grid" style="grid-template-columns: repeat(2, 1fr); gap: 14px;">
{% for r in rows %}
  <div class="card" style="border-left: 4px solid {% if r.status == 'done' %}#1b8a3a{% else %}#bbb{% endif %};">
    <div class="lbl">#{{ r.id }} · <span class="pill">{{ r.source }}</span></div>
    <div style="margin-top: 4px; font-size: 14px; font-weight: 600;">
      {% if r.status == 'done' %}✓{% else %}○{% endif %} {{ r.title }}
    </div>
    <div class="muted" style="margin-top: 6px;">{{ r.detail }}</div>
  </div>
{% endfor %}
</div>

<h2>How to update manual flags</h2>
<p>Edit <code>~/.hermes/paid/settings.json</code>:</p>
<pre style="background:#fff; padding:10px; border:1px solid #e3e3e3; border-radius:6px; font-size:12px;">
{
  "metrics_progress": {
    "cross_org_demo_done": true,
    "twitter_long_post_done": false,
    "deep_chats_count": 3,
    "readme_for_strangers_done": false
  }
}
</pre>
<p class="stamp">Then refresh this page.</p>
</body></html>
"""

_TRENDS_TEMPLATE = _BASE_HEAD + """\
<h1>Trends</h1>
<p class="stamp">Aggregated per UTC day from audit_log.jsonl + cost_ledger.jsonl.</p>

<div class="chartwrap">
  <h3>7-day decisions
    <span class="stamp" style="font-weight: normal; margin-left: 8px;">
      total: {{ t7.totals.direct }} direct / {{ t7.totals.request }} request / {{ t7.totals.decline }} decline
    </span>
  </h3>
  <canvas id="trend7Decisions" class="chart-lg"></canvas>
  <div id="trend7Fallback" class="chart-fallback" style="display:none">
    Chart library failed to load — see <a href="/api/trends.json">/api/trends.json</a>.
  </div>
</div>

<div class="chartwrap">
  <h3>7-day LLM cost (USD)
    <span class="stamp" style="font-weight: normal; margin-left: 8px;">
      week total: ${{ "%.2f" | format(t7.totals.cost_usd) }}
    </span>
  </h3>
  <canvas id="trend7Cost" class="chart-lg"></canvas>
</div>

<div class="chartwrap">
  <h3>30-day decisions
    <span class="stamp" style="font-weight: normal; margin-left: 8px;">
      total: {{ t30.totals.direct }} direct / {{ t30.totals.request }} request / {{ t30.totals.decline }} decline
    </span>
  </h3>
  <canvas id="trend30Decisions" class="chart-lg"></canvas>
</div>

<div class="chartwrap">
  <h3>30-day LLM cost (USD)
    <span class="stamp" style="font-weight: normal; margin-left: 8px;">
      month total: ${{ "%.2f" | format(t30.totals.cost_usd) }}
    </span>
  </h3>
  <canvas id="trend30Cost" class="chart-lg"></canvas>
</div>

<p class="stamp"><a href="/api/trends.json">Raw JSON</a></p>

<script>
(function() {
  if (typeof Chart === "undefined") {
    document.getElementById("trend7Fallback").style.display = "block";
    return;
  }
  var decisionsCfg = function(labels, direct, request, decline) {
    return {
      type: "line",
      data: {labels: labels, datasets: [
        {label: "direct",  data: direct,  borderColor: "#1b8a3a", tension: 0.2},
        {label: "request", data: request, borderColor: "#e0a020", tension: 0.2},
        {label: "decline", data: decline, borderColor: "#c33",    tension: 0.2}
      ]},
      options: {responsive: true, maintainAspectRatio: false,
                plugins: {legend: {position: "bottom"}},
                scales: {y: {beginAtZero: true, ticks: {precision: 0}}}}
    };
  };
  var costCfg = function(labels, costs) {
    return {
      type: "bar",
      data: {labels: labels, datasets: [
        {label: "cost USD", data: costs, backgroundColor: "rgba(34,102,204,0.6)", borderColor: "#2266cc"}
      ]},
      options: {responsive: true, maintainAspectRatio: false,
                plugins: {legend: {display: false}},
                scales: {y: {beginAtZero: true,
                             ticks: {callback: function(v){return "$"+v.toFixed(2);}}}}}
    };
  };
  new Chart(document.getElementById("trend7Decisions"),
    decisionsCfg({{ t7.labels|tojson }}, {{ t7.direct|tojson }}, {{ t7.request|tojson }}, {{ t7.decline|tojson }}));
  new Chart(document.getElementById("trend7Cost"),
    costCfg({{ t7.labels|tojson }}, {{ t7.cost_usd|tojson }}));
  new Chart(document.getElementById("trend30Decisions"),
    decisionsCfg({{ t30.labels|tojson }}, {{ t30.direct|tojson }}, {{ t30.request|tojson }}, {{ t30.decline|tojson }}));
  new Chart(document.getElementById("trend30Cost"),
    costCfg({{ t30.labels|tojson }}, {{ t30.cost_usd|tojson }}));
})();
</script>
</body></html>
"""

_PENDING_TEMPLATE = _BASE_HEAD + """\
<h1>Pending approvals ({{ pendings|length }})</h1>
{% if not pendings %}<p>(none)</p>{% else %}
{% for r in pendings %}
<div class="card" style="margin-bottom:14px;">
  <div class="stamp">#<code>{{ r.request_id }}</code> · {{ r.counterparty_display or r.counterparty_user_id }}
       ({{ r.counterparty_platform }}) · topic <b>{{ r.topic }}</b>
       · stakes {{ r.stakes }} · conf {{ "%.2f" | format(r.confidence) }}</div>
  <p><b>Q:</b> {{ r.junior_question }}</p>
  <p><b>Draft:</b> {{ r.draft_answer or "(no draft)" }}</p>
  <p class="stamp">resolve via slash: <code>/paid-approve {{ r.request_id }}</code> or
                    <code>/paid-reject {{ r.request_id }}</code></p>
</div>
{% endfor %}
{% endif %}
</body></html>
"""

_AUDIT_TEMPLATE = _BASE_HEAD + """\
<h1>Audit — {{ used_date }}</h1>
<form method="get"><label>UTC date:
  <input type="date" name="date" value="{{ used_date }}">
  <button>view</button></label></form>
<p>Showing newest {{ rows|length }} rows.</p>
<table><tr><th>ts</th><th>cp</th><th>state</th><th>topic</th><th>conf</th><th>q</th><th>resp preview</th></tr>
{% for r in rows %}
{% set act = r.action or {} %}{% set cls = r.classification or {} %}{% set ext = r.extra or {} %}
<tr>
  <td class="stamp">{{ r.ts[:19] }}</td>
  <td>{{ r.counterparty or "—" }}</td>
  <td>{{ act.state or "—" }}</td>
  <td>{{ cls.topic or "—" }}</td>
  <td>{{ "%.2f" | format(cls.confidence|default(0)) }}</td>
  <td>{{ (r.junior_msg or "")[:120] }}</td>
  <td>{{ (ext.assistant_response_preview or "")[:120] }}</td>
</tr>{% endfor %}</table>
</body></html>
"""

_FATAL_TEMPLATE = _BASE_HEAD + """\
<h1>Fatal alerts (newest first, max 50)</h1>
{% if not rows %}<p>(none)</p>{% else %}
<table><tr><th>ts</th><th>reason</th><th>detail</th></tr>
{% for r in rows %}<tr>
  <td class="stamp">{{ r.ts or "" }}</td>
  <td>{{ r.reason or "" }}</td>
  <td><code>{{ (r.detail or r.snippet or "")[:240] }}</code></td>
</tr>{% endfor %}</table>
{% endif %}
</body></html>
"""


def serve(host: str = "127.0.0.1", port: int = 7777, debug: bool = False) -> None:
    """Run the Flask dev server. Loopback-only by default."""
    app = build_app()
    print(f"PAID dashboard → http://{host}:{port}/  (Ctrl-C to stop)")
    app.run(host=host, port=port, debug=debug, use_reloader=False)
