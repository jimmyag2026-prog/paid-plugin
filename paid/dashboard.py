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
    audit = _read_jsonl(storage.PAID_DIR / "audit_log.jsonl")
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
        "l1_hits_today": l1,
        "l4_hits_today": l4,
        "classifier_fallback_today": fb,
        "pending_count": len(approval.list_pending()),
        "audit_rows_total": len(audit),
    }


def collect_counterparties() -> list[dict[str, Any]]:
    """All cps with last-seen + activity-dot, newest first."""
    audit = _read_jsonl(storage.PAID_DIR / "audit_log.jsonl")
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
    audit = _read_jsonl(storage.PAID_DIR / "audit_log.jsonl")
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

    app = Flask("paid-dashboard")
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
                "q_preview": r.junior_question[:120],
            }
            for r in approval.list_pending()
        ]
        return render_template_string(_HOME_TEMPLATE, s=s, cps=cps[:10], pending=pending)

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
        audit = _read_jsonl(storage.PAID_DIR / "audit_log.jsonl")
        rows = [r for r in audit if r.get("counterparty") == cp_id]
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return render_template_string(_CP_DETAIL_TEMPLATE, cp=cp, rows=rows[:50])

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
  </style>
</head><body>
<nav>
  <a href="/">Summary</a>
  <a href="/counterparties">Counterparties</a>
  <a href="/pending">Pending</a>
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
  <td>{{ (r.junior_msg or "")[:140] }}</td>
</tr>{% endfor %}</table>
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
