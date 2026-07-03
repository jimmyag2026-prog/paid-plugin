---
status: DRAFT — needs Jimmy review before coding
target_version: 1.7.0
target_date: TBD (gated on Slack workspace availability for live smoke)
sibling_doc: design/05_backlog.md §M3.1 + §M3.5.C-slack
prior_art: commit 19f7641 (v1.4.0 — TG button callback, M3.5.C)
---

# v1.7.0 — Slack 端到端打通 + Owner 主通讯频道选择（M3.1 + M3.5.C-slack + 新 §M2.7.1）

## 1. 目标

### 1.A · Slack 端到端打通

让 Slack 上的 PAID 体验跟 Lark/TG 对齐：

- ✅ approval card 已经能发（v1.2.0 完工）
- ✅ Block Kit 按钮 ✅ Approve / ✏️ Reply / ❌ Reject 已渲染
- ❌ **按钮点击不响应** — 这是 v1.7 唯一要堵的缺口
- ❌ Slack 没有真 workspace 跑过端到端 smoke — pilot-ready 的前置

完工标准 (`master design §6` 第 3 条多平台兼容 + `backlog §M3.1` 验收):

1. 在真 Slack workspace 上，junior 触发 → owner 收 approval card → 点 ✅/✏️/❌ 三个按钮都正确路由到 PAID 主链路（不需要 fallback 到 slash command）
2. 点击后原 card 被 update 显示 resolution（跟 TG 行为对齐）
3. 非 owner 点击 → 收到 "Not authorized" ephemeral 提示，不触发任何 PAID 动作
4. options-block 按钮（`paid_opt_<key>`，review skill 用）暂延后 v1.7.2，本次不做
5. 全量 pytest 1192 → 1192 + N 全绿；无 v1.0.0 基线回归

### 1.B · Owner 主通讯频道选择（新加，与 Slack 上线 thematically 相关）

底层 plumbing **已经全有**（`Owner.preferred_platform` + `preferred_identity()` + `_notify_owner_about_request` 已实装），缺的是 UX：让 owner 显式选 "PAID 主要在哪个频道找我"。Slack 上线后 owner 真有了 3 个 channel 选项（Lark / TG / Slack），需要显式 surface。

完工标准：
1. owner 在 setup wizard 配置 ≥2 个 channel 时，wizard 主动问主频道
2. owner 用 `/paid-set-primary slack`（或 lark / telegram）能切换主频道
3. `/paid-status` 显示当前 primary channel
4. `/paid-doctor` 在"≥2 enabled identity 但 primary 没设"时给提示
5. owner 没显式选时 → 沿用现有"第一个 enabled identity" 行为（zero-regression）

## 2. 现状梳理

### 2.1 已有代码（v1.2.0–v1.6.6 累积）

| 文件 | 提供能力 |
|------|---------|
| `paid/card_formatters.py::format_slack` | 渲染 approval card → Block Kit JSON。Buttons: `action_id=paid_approve/paid_reply/paid_reject`，`value=request_id` |
| `paid/hermes_io.py::send_slack_block` | 通过 `adapter._app.client.chat_postMessage` 发 blocks；失败降级到 `outbound_queue.jsonl` |
| `paid/hermes_io.py::_options_block_to_slack_blocks` | review skill 用，options → `action_id=paid_opt_<key>`，`value=key` |
| `paid/hermes_io.py::send_dm` | platform="slack" 分支已实装 |
| `tests/test_send_telegram_slack.py` etc. | format_slack + send_slack_block 单测齐 |

### 2.2 Hermes Slack adapter 表面（`gateway/platforms/slack.py`）

- 内部用 `slack_bolt.async_app.AsyncApp` 作为 `self._app`
- 启动时（行 631-646）只注册了自己的 `hermes_*` action_id：
  - `hermes_approve_once / session / always / deny`
  - `hermes_confirm_once / always / cancel`
- **`paid_*` 前缀完全空着**，没有命名冲突
- Bolt 的 `app.action(action_id_or_pattern)(handler)` 是 plugin 唯一进入点
- Bolt 没有 PTB 的 `group` 优先级概念 — 但因为 action_id 命名空间不冲突，不需要优先级

### 2.3 跟 TG callback (v1.4.0) 的差异

| 维度 | TG (v1.4.0 已做) | Slack (本次要做) |
|------|----------|------|
| Adapter 内部对象 | `adapter._app` (python-telegram-bot Application) | `adapter._app` (slack_bolt AsyncApp) |
| 注册 API | `app.add_handler(CallbackQueryHandler(fn, pattern=r"^paid_"), group=-1)` | `app.action({"action_id": re.compile(r"^paid_")})(fn)` |
| 冲突隔离 | `group=-1` 先于 hermes catch-all 跑 | action_id 前缀隔离 — `paid_*` vs `hermes_*` |
| 事件 ack | `await query.answer()` | `await ack()` |
| 解析 button 元数据 | `query.data = "paid_approve:<rid>"` | `body["actions"][0]` → `{"action_id": "paid_approve", "value": "<rid>"}` |
| Owner 鉴权 | `query.from_user.id` vs `identity.is_owner("telegram", uid)` | `body["user"]["id"]` (Slack `U...`) vs `identity.is_owner("slack", uid)` |
| 卡片 update | `query.edit_message_text(text, reply_markup=None)` | `client.chat_update(channel=..., ts=..., blocks=[...], text=...)` |
| Edit 失败 fallback | `context.bot.send_message(chat_id, text)` | `client.chat_postMessage(channel, text)` |
| 失败时仍 ack | TG 自动；Bolt 必须显式 `await ack()` 否则 spinner 不消 | 同左 |

**核心结论**：架构跟 TG 完全对称，只是 SDK API 名字变。**~70% 代码可以直接镜像 TG path**。

## 3. 实施拆分（v1.7.0 + v1.7.1）

### v1.7.0 — 主路径（Approve / Reply / Reject 三键）

#### 3.1 文件改动清单

**A. `paid/__init__.py`** — 主要增量 (~250 行，镜像 TG 的 `_ensure_telegram_callback_registered` + `_on_paid_telegram_callback`)

新增函数：
- `_ensure_slack_callback_registered()` — 镜像 TG 等价物。
  - 调用：`adapter = hermes_io._get_gateway_adapter("slack")` → `app = adapter._app`
  - 导入：`from slack_bolt.async_app import AsyncApp` 校验，失败 `_alert_owner` 且不重试
  - 注册：用 Bolt `constraints` dict 模式
    ```python
    import re
    app.action({"action_id": re.compile(r"^paid_")})(_on_paid_slack_action)
    ```
  - Idempotent：复用现有 `_callback_registered: dict[str, bool]`，加 key `"slack"`
  - 锁：复用 `_CALLBACK_LOCK`

- `_on_paid_slack_action(ack, body, client, logger=None)` — async handler
  1. `await ack()` 第一时间消 Slack UI 三角形 loading
  2. 解析 `action = body["actions"][0]`：
     - `action_id` = `paid_approve` / `paid_reply` / `paid_reject` / `paid_opt_<key>`
     - `value` = request_id（approve/reply/reject）或 key（options block）
  3. Owner 校验：`identity.is_owner("slack", body["user"]["id"])`
     - 非 owner → `client.chat_postEphemeral(channel=container.channel_id, user=user_id, text="⛔ Not authorized")` + log + return
  4. `loop.run_in_executor` 调度同步 `_do_approve` / `_do_reject` / `_record_awaiting_input`
     - 同 TG：避免在 gateway event loop 里裸跑 `send_dm`（它内部 `run_coroutine_threadsafe + .result()` 会死锁）
  5. 完事 update card：
     - 取 `container.channel_id` + `container.message_ts` 从 `body`
     - `await client.chat_update(channel=..., ts=..., blocks=[简化版含 resolution], text=fallback_text)`
     - 失败 → `await client.chat_postMessage(channel, text=result_text)`

- `_parse_paid_slack_action(action_id: str, value: str) -> tuple[str, str] | None`
  - 镜像 `_parse_paid_callback_data` 但适配 Slack 的 (action_id, value) 双字段
  - 白名单：`approve / reject / reply / opt` （Slack 永远走 `paid_reply`，不存在 v1.4 前的 `edit` 老 alias）
  - opt：`paid_opt_<key>` 形态 → action="opt"，rid=key

修改函数：
- `on_pre_gateway_dispatch` / `on_pre_llm_call` 里 platform="telegram" 调 `_ensure_telegram_callback_registered()` 的两处，加 platform="slack" 等价分支调 `_ensure_slack_callback_registered()`
  - 检查位置：grep 出来是 `__init__.py:777` 和 `__init__.py:1206`

**B. `paid/card_formatters.py`** — docstring 更新 (~10 行)

- 顶部模块 docstring 第 17-18 行 "Slack click routing: planned v1.4.x..." 改为 "Slack click routing: ✅ v1.7.0 (M3.5.C-slack)"
- `format_slack` action button 处 "click is NOT routed to PAID" 注释 → "click routed via `_ensure_slack_callback_registered` in `__init__.py`"
- 0 行为变化 — action_id 已经是 `paid_approve` 等，formatter 不动

**C. `plugin.yaml`** — version 1.6.0 → 1.7.0

**D. `README.md`** — 加 v1.6 → v1.7 changelog 段，platform support table 把 Slack 行从 🟡 改 ✅

#### 3.2 文件改动清单（Owner Primary Channel — §1.B）

**E. `paid/profile.py`** — schema 加字段 (~5 行)
- `OwnerProfile` dataclass 加 `preferred_platform: str = ""` 字段
- load/save tolerate 老 profile（缺字段 → 空 string）
- `ALLOWED_PROFILE_FIELDS` 加 `"preferred_platform"`

**F. `paid/profile_sync.py::_write_owner_json`** — 优先级调整 (~10 行)
- 当前逻辑 (`profile_sync.py:98-103`): 取 `identities[]` 第一个 enabled 的 platform
- 新逻辑：
  ```python
  preferred = profile.preferred_platform.strip()
  if not preferred:
      # backward-compat fallback: first enabled identity
      for ident in profile.identities:
          if isinstance(ident, dict) and ident.get("enabled", True):
              preferred = ident.get("platform", "")
              if preferred:
                  break
  ```
- 老 profile 行为不变；显式设过 preferred 的 owner 优先

**G. `paid/setup_wizard.py`** — 加 Q6 + edit 菜单项 (~50 行)
- `_FIRST_TIME_FLOW`（or wizard step list）加一步 `ask_preferred_platform`：
  - 触发条件：检测到 owner.identities 已有 ≥2 个 enabled identity（v1.6 wizard 当前不主动加 identity，所以这步只在 edit 模式才有意义；但 v1.7 也要解锁 first-time 路径——见下文 "wizard auto-add identity" 联动）
  - 提示文案："PAID 现在能在 {channels} 找你，想让通知主要发到哪个？(回数字或平台名；回 'skip' 跳过让 PAID 自己挑)"
  - 校验：必须是 owner 已有 identity 的 platform
- `_render_edit_menu` 加第 6 项 "主频道"，显示当前 `prof.preferred_platform or '(自动)'`
- `_apply_answer` 加 `preferred_platform` 字段 handler

**H. `paid/__init__.py`** — 加 `/paid-set-primary` slash command (~30 行)
- 注册：`ctx.register_command("paid-set-primary", _cmd_set_primary)`
- handler：
  - 校验 owner via `_is_caller_owner_via_env`
  - 解析平台名（lark / telegram / slack / feishu 容忍）
  - 校验该 platform 在 owner.identities 里存在且 enabled
  - 写 `OwnerProfile.preferred_platform` → 调 `profile.save_profile` → `profile_sync.derive_from_profile`
  - 回复 "✓ 主频道已切换到 {platform}"
- `_cmd_status` 增量：在输出里加一行 "主频道: {primary} (其他 enabled: ...)"

**I. `paid/doctor.py`** — 加 1 条 check (~10 行)
- check id `primary_channel_set`
- 触发：`enabled_identities ≥2 且 owner.preferred_platform 空`
- 结果：warn (not error)，文案 "你有 N 个 channel enabled 但没指定主频道，PAID 默认用 {first}。运行 /paid-set-primary <platform> 改"
- 加进 `run_checks` 列表

**J. `tests/`** — 新测试 (~100 行)
- `tests/test_owner_primary_channel.py`：
  - `test_profile_persists_preferred_platform`
  - `test_profile_sync_prefers_explicit_over_first_identity`
  - `test_profile_sync_falls_back_when_explicit_empty`（zero-regression 验证）
  - `test_cmd_set_primary_happy`
  - `test_cmd_set_primary_unknown_platform` → 报错不写
  - `test_cmd_set_primary_disabled_identity` → 报错不写
  - `test_cmd_set_primary_non_owner` → silent ignore
  - `test_doctor_primary_warns_when_multi_identity_unset`
  - `test_doctor_primary_silent_when_single_identity`

#### 3.3 wizard auto-add identity 联动（顺手收掉 TODO）

`setup_wizard.py:466` 的老 TODO "v1.6.x: have wizard auto-add the platform+sender_id of the wizard invocation event as the first identity" —— **正好跟 §1.B 同方向**：wizard 跑完没 identity 的话，1.B 的 Q6 也问不出来。

借 v1.7 一起做：
- 在 `_finalize_first_time` 里把 caller event 的 `(platform, sender_id)` 自动 append 到 `prof.identities` 作为第一条
- caller event 来源：wizard `start()` 已经接 `platform` 参数，sender_id 需要 caller 多传一个；改 `start()` signature
- 一次性同步改 wizard 调用方（`__init__.py` 的 `_cmd_paid_setup`）传 sender_id

**B 块（§1.B）跟主任务 A 块（Slack callback）正交**，可以并行写，单独 commit。

TG path 在 `_on_paid_telegram_callback` 里 `_record_awaiting_input(rid, expected_chat_id=...)` 已经实装；下游 `on_pre_gateway_dispatch` 消费 next plain-text。Slack 走同一个 `_record_awaiting_input` 入口即可 —— 但需要确认 Slack 的 `expected_chat_id` 取什么：

- TG：`query.message.chat_id`（数字）
- Slack：`body["container"]["channel_id"]`（`D...` 形式 DM channel）

`_record_awaiting_input` 当前只关心字符串 chat_id，应该不区分 platform 直接 work。**v1.7.0 里同步实装即可，不拆 v1.7.1**——除非测试时发现 `on_pre_gateway_dispatch` 里有 platform-specific 假设需要解锁。

实际把 v1.7.0 + v1.7.1 合并成 **v1.7.0 一次发**。

### v1.7.2 — Options-block button 路由（review skill 选项点击）

延后单独 v1.7.2，因为：
- 需要 review skill 侧也消费 button click（不是 PAID 主路径）
- v1.7.0 主目标先解决 approval card 三键
- options click 仍可走 plain-text reply fallback（用户输入 `a` / `pass` 等）

### v1.7.x 之后（不在本次范围）

- Slack 多 workspace 支持（`_team_clients` 字典已存在）
- Slack 群聊 @ 触发（M3.3）
- Slack 多平台 cp 身份聚合（M3.4）

## 4. 测试计划

### 4.1 自动测试（必跑，PR 必绿）

**新文件 `tests/test_slack_callback.py`** — 镜像 `test_tg_callback.py`（463 行模板），预估 ~400 行：

| 测试 | 内容 |
|------|------|
| `test_register_idempotent` | 重复调 `_ensure_slack_callback_registered` 不重复注册 |
| `test_register_no_adapter` | adapter 缺失 → 不 set flag（保留重试机会）|
| `test_register_no_app` | adapter 在但 `_app=None` → 同上 |
| `test_register_slack_bolt_missing` | mock ImportError → flag 强 set + `_alert_owner` 1 次 |
| `test_register_add_handler_raises` | mock `app.action` raise → flag 强 set + `_alert_owner` |
| `test_action_parse_approve` | `(action_id="paid_approve", value="rid8char")` → `("approve","rid8char")` |
| `test_action_parse_reject` | 同上 reject |
| `test_action_parse_reply` | 同上 reply |
| `test_action_parse_opt` | `paid_opt_a` → `("opt","a")` |
| `test_action_parse_malformed` | 各种坏输入 → None |
| `test_action_parse_whitelist` | `paid_foo` → None |
| `test_handler_approve_happy` | fake Slack body → `_do_approve` 调用 + chat_update 触发 |
| `test_handler_reject_happy` | 同上 reject |
| `test_handler_reply_arms_input` | 点 reply → `_record_awaiting_input(rid, expected_chat_id=...)` 被调 |
| `test_handler_non_owner_ephemeral` | identity.is_owner=False → `chat_postEphemeral` + 不调 _do_* |
| `test_handler_acks_first` | ack 在 owner 校验之前 await（防 Slack UI 卡 3s 超时）|
| `test_handler_dispatch_exception` | _do_approve raise → card update 仍然发，含 error |
| `test_handler_chat_update_fail_falls_back` | chat_update 抛异常 → chat_postMessage 兜底 |

mock 策略：
- 不真起 slack_bolt — `unittest.mock.patch("slack_bolt.async_app.AsyncApp")` + 假 `AsyncApp` 类暴露 `.action / .client.chat_update / .client.chat_postMessage / .client.chat_postEphemeral`
- fake body 用 Slack 官方 payload 形态（参 `tests/test_slack_approval_buttons.py` in hermes-agent 找现成 fixture）

**回归套**：
- 跑全量 `python3 -m pytest tests/` 必须 1192 + N 全绿
- 特别关注 `test_send_telegram_slack.py` / `test_tg_callback.py` 不退化（共享 `_CALLBACK_LOCK` + `_callback_registered`）

### 4.2 手动 smoke（gated on Slack workspace）

按 `feedback_im_bot_api_traps` 必跑（mock 掩盖真错）：

1. **环境准备** (~30 min)
   - Slack workspace（自己开 free tier，或借朋友 sandbox）
   - 在 api.slack.com/apps 建 app：
     - Socket Mode ON
     - Bot token scopes: `chat:write`, `chat:write.public`, `im:history`, `im:write`, `users:read`, `app_mentions:read`, `commands`
     - Interactivity ON, Request URL 不填（Socket Mode 用 WS）
   - 安装到 workspace → 拿 `xoxb-...` (bot token) + `xapp-...` (app token)
   - `~/.hermes/.env` 解开两个 commented `SLACK_*_TOKEN`
   - 加 Slack identity 到 `owner.json`：`{"platform":"slack","user_id":"U..."}`
   - `hermes-gateway restart`

2. **Smoke checklist**：
   - [ ] junior 用第二个 Slack 账号给 bot 发 DM "Q3 预算怎么排？" → owner DM 收到 approval card
   - [ ] 卡片 ✅ Approve / ✏️ Reply / ❌ Reject 三键都渲染
   - [ ] 点 ✅ → junior 真收到 PAID draft；owner 端 card update 显示 "PAID: #rid approved → delivered to ..."
   - [ ] 点 ❌ → junior 收到 deflection；card update 显示 "PAID: #rid rejected"
   - [ ] 点 ✏️ → owner 收 ephemeral "✏️ #rid 等你输入..." → 发下一条文字 → junior 收到该文字 → card update 显示 delivered
   - [ ] **从 non-owner Slack 账号点 ✅** → 收到 "⛔ Not authorized" ephemeral；PAID 不动
   - [ ] **gateway restart 中途**：重启后下一个 inbound 触发 `_ensure_slack_callback_registered` 重新注册；再点旧 card 仍能 work
   - [ ] **跨平台不污染**：smoke 期间用 Lark 账号发 J2 三态走老路径，验证 v1.0.0 基线没退化（pytest 179 + manual_smoke 48 简易抽样）

3. **smoke 出问题立刻 revert** —— v1.7 改动全在 `__init__.py` 末尾 + 一个新测试文件，revert 范围明确

### 4.3 测试 gate

- 自动测试 100% green = 进 manual smoke 前置门
- Manual smoke 7/7 = release 门
- 任一项挂 = revert，不发布

## 5. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| Bolt `app.action(regex)` 注册 API 在某版本有差异 | 测试用 fake AsyncApp 覆盖；live smoke 兜底；slack_bolt 1.x 系列文档明确支持 dict constraint |
| `body["container"]` 字段在 attachment / blocks_actions / block_suggestion 不一致 | 只处理 `block_actions` payload；其他类型早 return；测试覆盖 |
| owner.json 没 Slack identity → 所有点击都 unauthorized | doctor.py 加一条 check："slack 启用但 owner 没 Slack identity"；smoke 前置 manual 加 |
| Slack ack 必须 3s 内 → 同步链路慢会超时 | ack 第一行就 await；业务跑 `run_in_executor` 后台 |
| 多 workspace（`_team_clients`）选 client 走哪个 | v1.7 只支持单 workspace（同 v1.2 send_slack_block 的边界）；多 workspace 标为 v1.7.x backlog |
| 老 card（>3 天）`chat_update` 失败 | 已有 fallback：失败转 `chat_postMessage` 发新消息 |
| 共享 `_callback_registered` dict 给 slack key 时跟 telegram 互相干扰 | dict key 隔离；单测覆盖一个 platform 注册不影响另一个 |

## 6. 显式 out of scope

- ❌ Slack Socket Mode → HTTP mode 切换（hermes 决定，不在 PAID）
- ❌ Slack 多 workspace 同时跑（v1.7.x）
- ❌ Slack 群聊 @ trigger（M3.3，独立 milestone）
- ❌ Slack cp 跨平台身份聚合（M3.4）
- ❌ Options-block 按钮 click 路由（v1.7.2 拆出去）
- ❌ Slack 富 modal（Z form / Block Kit modal）— pure card-button only

## 7. 时间预估

| 阶段 | 估时 |
|------|------|
| 设计 review + 改 | 1h（本次产出） |
| §1.A Slack callback 实施代码（`__init__.py` 250 行）| 3-4h |
| §1.A 自动测试 18 用例（~400 行）| 3-4h |
| §1.B Owner primary channel 实施（profile / wizard / cmd / doctor）| 2-3h |
| §1.B 自动测试 9 用例（~100 行）| 1-2h |
| 自动测试 debug 到全绿 | 1-2h |
| README + plugin.yaml + docstring | 0.5h |
| Manual smoke（需 workspace 就绪）| 1-2h |
| **总计** | **12-18h**，假设 workspace 当天能配好；否则 dev 完成 + 等 workspace 后做 smoke |

## 8. Ship 后跟进

- backlog M3.1 / M3.5.C-slack 标 ✅ done
- post-ship 第一周 dogfood：用 Slack 跑 ≥1 个真任务全周期；切一次 primary channel 验证生效
- memory 加一条 `reference_slack_setup.md`（workspace 配置流程，方便 pilot 复用）
- 评估 v1.7.2（options-block 按钮路由）是否真需要 —— dogfood 看是否有 review skill 用户卡在文字回复 fallback
- §1.B 之后是否需要 `auto_route_by_topic`（不同话题路由不同 channel）—— 留作 v1.8 backlog，不在 v1.7 范围
