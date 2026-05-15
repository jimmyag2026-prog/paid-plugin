---
status: APPROVED — implementing on v1.6.8 base
target_version: 1.7.0
base_commit: cf0b69e (fix/v1.6.8-blockers)
workspace: ~/Desktop/paid-plugin-v1.7/ (git worktree, isolated from main paid-plugin testing)
sibling_doc: design/05_backlog.md §M3.1 + §M3.5.C-slack
prior_art: commit 19f7641 (v1.4.0 — TG button callback, M3.5.C)
---

# v1.7.0 — Slack 端到端打通 + Owner 主通讯频道选择

## 1. 目标

### 1.A — Slack 端到端打通（M3.1 + M3.5.C-slack）

让 Slack 上的 PAID 体验跟 Lark/TG 对齐：

- ✅ approval card 已经能发（v1.2.0 完工）
- ✅ Block Kit 按钮 ✅ Approve / ✏️ Reply / ❌ Reject 已渲染
- ❌ **按钮点击不响应** — 这是 v1.7 要堵的缺口
- ❌ Slack 没有真 workspace 跑过端到端 smoke

完工标准：
1. 真 Slack workspace 上，junior 触发 → owner 收 card → 点 ✅/✏️/❌ 都路由到 PAID 主链路
2. 点击后原 card 被 chat_update 显示 resolution
3. 非 owner 点击 → ephemeral "Not authorized"，不触发任何 PAID 动作
4. options-block 按钮（`paid_opt_<key>`）延后 v1.7.2，本次不做
5. 全量 pytest 1192 + N 全绿；v1.6.8 baseline 0 退化

### 1.B — Owner 主通讯频道选择

底层 plumbing 全有了（`Owner.preferred_platform` + `preferred_identity()` + `_notify_owner_about_request`），缺 UX：让 owner 显式选 "PAID 主要在哪个频道找我"。

完工标准：
1. owner 在 setup wizard 配置 ≥2 channel 时主动问主频道
2. `/paid-set-primary <platform>` 切换主频道
3. `/paid-status` 显示当前 primary channel
4. `/paid-doctor` warn "≥2 enabled identity 但 primary 没设"
5. zero-regression：没显式选时沿用"第一个 enabled identity"

### v1.6.8 base 影响（vs 原 v1.6.7 设计）

⚠️ **`setup_wizard._finalize_first_time` 在 v1.6.8 改了**：从 `new_profile()` 重建 → 改成对 existing profile 做 PARTIAL update（保留 wizard 没问的字段，比如 migration 从 sop.md 抽出的 always_decline）。

§1.B 的两件事必须基于 partial-update 形态实施：
- **Auto-add identity**：在 partial-update 路径里追加，不动其他字段
- **Q6 preferred_platform**：成为第 6 题（条件触发：identities ≥ 2），同样走 partial-update

§1.A 跟 v1.6.8 完全正交，零冲突。

## 2. 实施清单

### §1.A · Slack callback (`__init__.py` + `card_formatters.py` + 新 test)
- `_ensure_slack_callback_registered()` — 镜像 TG 等价物，调 `app.action({"action_id": re.compile(r"^paid_")})(handler)`
- `_on_paid_slack_action(ack, body, client, logger=None)` — 异步 handler
  1. `await ack()` 第一时间
  2. 解析 body["actions"][0] → (action_id, value)
  3. owner 校验 `body["user"]["id"]` → 非 owner ephemeral
  4. `run_in_executor` 调度 `_do_approve` / `_do_reject` / `_record_awaiting_input`
  5. `client.chat_update` 更新 card；失败 fallback `chat_postMessage`
- `_parse_paid_slack_action(action_id, value)` — 白名单解析
- 触发点：`on_pre_gateway_dispatch` + `on_pre_llm_call` 两处加 platform="slack" 分支
- `paid/card_formatters.py` docstring：模块 + `format_slack` + action button 注释更新
- 新文件 `tests/test_slack_callback.py` — 19 个 unit test

### §1.B · Owner Primary Channel
- `paid/profile.py`: 加 `OwnerProfile.preferred_platform: str = ""` + ALLOWED_PROFILE_FIELDS + load 解析
- `paid/profile_sync.py::_write_owner_json`: 优先 `profile.preferred_platform`，缺省 fallback 到 identities[0]
- `paid/setup_wizard.py`:
  - 在 v1.6.8 partial-update `_finalize_first_time` 里追加 auto-add caller identity（如果 identities 为空）
  - 追加 Q6 prompt + answer apply（条件触发：post-add 后 enabled identities ≥ 2）
- `paid/__init__.py`:
  - 新 slash command `/paid-set-primary <platform>` handler
  - `/paid-status` 输出加一行 primary channel
- `paid/doctor.py`: 加 check `primary_channel_set`（warn level）
- 新文件 `tests/test_owner_primary_channel.py` — 9 个 unit test

### Finalize
- `plugin.yaml` version 1.6.8 → 1.7.0
- `README.md` changelog + Slack 行从 🟡 改 ✅
- `python3 -m pytest tests/` 全绿
