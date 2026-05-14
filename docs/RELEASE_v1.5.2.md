# PAID v1.5.2 — Lark card-click P2P regression fix

**Released:** 2026-05-14
**Triggered by:** 2026-05-14 live manual test on paid uid 1002 VPS

Hot-fix for a v1.5.0-introduced regression that blocked **all Lark
interactive card button clicks** (✅ / ✏️ / ❌) in owner P2P DMs.

---

## The bug

Symptom: Owner clicks ✅ on a PAID approval card in Lark DM with the
bot — nothing happens. PAID's `_cmd_card` handler never fires. pending
status stays `None`.

Investigation (2026-05-14):

- Lark WebSocket DID receive the card event.
- lark-oapi DID dispatch to hermes adapter (`_handle_card_action_event`).
- hermes adapter DID synthesize a `/card button {json}` MessageEvent
  and emit `[Feishu] Routing card action 'button' from ou_... in oc_...
  as synthetic command` (28 of them on paid alone on 2026-05-14, all
  during manual test).
- But the next log line was always:

  ```
  pre_gateway_dispatch skip: reason=paid_group_not_enabled
    platform=feishu chat=oc_...
  ```

- PAID's `on_pre_gateway_dispatch` rejected every click at the Phase 6
  group-routing gate.

**Root cause**: hermes feishu adapter
(`gateway/platforms/feishu.py::_handle_card_action_event`) hardcodes
`event_chat_type="group"` for every synthesized card-action command,
regardless of whether the click came from a real group or an owner's
private DM. PAID v1.5.0's Phase 6 `classify_chat()` trusted that field
→ classified the click as a group message → unconfigured group →
`group_disabled` → drop.

v1.4.x had no Phase 6 routing, so paid-jelabs (running v1.4.5) was
unaffected. The bug is exclusive to v1.5.0+.

## The fix

In `on_pre_gateway_dispatch`, short-circuit the routing gate when the
event text starts with `/card ` (the synthetic-command prefix hermes
uses). Those events MUST go straight to the slash dispatcher → PAID's
`_cmd_card`.

```python
event_text_peek = str(getattr(event, "text", "") or "")
if event_text_peek.lstrip().startswith("/card "):
    _routing = "p2p"  # bypass group routing entirely
else:
    _routing = group_routing.classify_routing(event, text=event_text_peek)
```

Code change: ~5 lines in `__init__.py`. No API surface change.

## Tests

`tests/test_group_routing_hook.py` adds 2 regression tests:

- `test_card_synthetic_command_bypasses_group_routing` — simulates the
  exact hermes synthetic event (chat_type='group' hardcoded) for an
  owner DM card click, verifies it falls through to `_cmd_card`
  instead of being dropped.
- `test_card_synthetic_command_bypasses_review_only_strict` — same
  invariant in review-only group context (the v1.5.1 strict mode
  shouldn't gate card clicks either).

`843 passed, 1 skipped` — full suite green. +2 net vs v1.5.1.

## Why it took two days to find

The hermes INFO log `[Feishu] Routing card action ...` writes to
`~/.hermes/logs/gateway.log` via Python's logger, NOT to systemd's
journal. During manual testing I only watched `journalctl --user -u
hermes-gateway.service` and reported "0 events to WS" — which made me
chase down a wrong lark-oapi `MessageType.CARD: return` rabbit hole and
even monkey-patch lark-oapi for no reason. The real signal was in
`gateway.log` the whole time.

**Lesson** (saved to memory): on this VPS, hermes-gateway INFO logs go
to `~/.hermes/logs/gateway.log`, not journal. Always check both during
diagnostics.

## Deploy

Same dep surface as v1.5.0/v1.5.1 — no new apt or pip deps.

```bash
# On VPS paid user (uid 1002):
rsync ./ root@159.65.75.97:/tmp/paid-v1.v1.5.2-stage/ \
    --exclude='.git' --exclude='__pycache__' --exclude='*.pyc'
ssh root@159.65.75.97 'chown -R paid:paid /tmp/paid-v1.v1.5.2-stage'
sudo -u paid -H -- bash -c '
  cd ~paid/.hermes/plugins
  TS=$(date -u +%Y%m%dT%H%M%S)
  mv ~/paid-v1.replaced-*.bak.... 2>/dev/null
  mv paid-v1 ~paid/paid-v1.replaced-$TS
  mv /tmp/paid-v1.v1.5.2-stage paid-v1
  export XDG_RUNTIME_DIR=/run/user/1002
  systemctl --user restart hermes-gateway.service
'
```
