# PAID v1.5.5 — TODO 集合（plan，未实施）

**起草日期**：2026-05-14
**前版**：v1.5.4 (Lark attachment binding fix, 已 ship)
**来源**：2026-05-14 session 重审 `design/05_backlog.md` M2.x / M5 / M2.9 / M9.4
后达成的"还要做"清单。每项链回 backlog 原文 + 实施草图 + 验收门。

---

## 一、Scope 总览

v1.5.5 = **owner-facing 工具加强 + 安全防线收口**。不动 J2/J3/J4 主链路、
不动 review skill 内核（M1）、不动 IM 多平台 routing（M3）。

分两轨：

- **轨 A：paid-plugin 内**（v1.5.5 主 release）—— doctor 命令 / cost ceiling
  inline / dashboard A 段 + B 段 owner 宏观视图
- **轨 B：hermes upstream PR**（并行，可独立 merge）—— M5.2 sender_id 透传 /
  M5.3 adapter receive_id_type 不硬编码

轨 A 全部 zero hermes 依赖、向后兼容；轨 B 是给 v1.6+ 下线 PAID workaround
铺路，不阻塞 v1.5.5 ship。

---

## 二、轨 A · paid-plugin 内（按 ROI 排序）

### A1 · M2.7 `/paid-doctor` 命令  🟢 P1

**背景**：当前无 doctor / health-check 入口。下个 pilot onboard 时 operator
排查"为什么没收到 PAID 消息"得手动 grep 多个文件。

**实施**：
- 新建 `bin/doctor.py` —— CLI 入口 `python -m paid doctor`
- 新建 slash command `/paid-doctor`（注册在 `__init__.py` 现有 5 slash 旁边）
- 检查项：
  1. `~/.hermes/config.yaml` 存在 + model + base_url + api_key 全字段
  2. `~/.hermes/paid/owner.json` schema_version + identities 完整
  3. plugin.yaml `minimum_hermes_version` vs 实际 `ctx.register_command` 可用性
  4. systemd timer 三个（paid-sweep / paid-review-sweep / paid-daily-snapshot）状态
  5. `cost_ledger.jsonl` / `audit_log.jsonl` / `pending_approvals.jsonl` 可写
  6. settings.json schema 合法（已知字段 + 类型 + 范围）
  7. 最近 1h 内有无 fatal/error 事件（grep `_safe_log` 输出）

**验收**：
- `python -m paid doctor` 输出每项 `[✓]` / `[✗]` + 修复建议
- `/paid-doctor` 给 owner 一个简版 IM 摘要（fail-only）
- 7 个检查全过 = exit 0；任一失败 = exit 1

**冲突评估**：🟢 零冲突，纯加

---

### A2 · M9.4 cost ceiling inline enforce  🟢 P1

**背景**：M2.9 整模块作废后，cost ceiling 落点改为 `paid/hermes_io.py:333`
`call_llm` 入口直接检查。详 backlog M2.9 重审结论 + M9.4 段。

**实施**：

```python
# paid/hermes_io.py call_llm() 头部
def call_llm(prompt, system="", ...):
    from . import cost
    status = cost.cap_status()
    if status["enabled"] and status["daily_hard_exceeded"]:
        raise LLMCallError(
            f"PAID daily LLM budget exhausted "
            f"(${status['today_usd']:.2f} >= ${status['daily_hard_cap']:.2f}); "
            f"resets 00:00 UTC"
        )
    ...
```

加 once-per-day `_alert_owner` dedupe（用 `storage` 写一个 flag 文件
`cost_cap_alerted_<YYYY-MM-DD>.flag`，跨过日重置）。

J2 主链路 `pre_llm_call` catch `LLMCallError` → owner 收 alert IM +
junior 收"系统暂不可用"。

**验收**：
- 单测：mock `cap_status` 返回 `daily_hard_exceeded=True` → `call_llm` raise
- 单测：同一天多次触发 → owner 只收 1 次 alert
- 手动：临时把 daily_hard_cap_usd 调到 $0.01，发一条 junior IM → classifier
  路径就 raise，junior 收"系统暂不可用"，owner 收 alert IM

**冲突评估**：🟢 零冲突。classifier / safety / review 调用方现有
try/except 已 graceful degrade `LLMCallError`

---

### A3 · M2.6.A 顶部 metric 条 + question preview  🟢 P2

**背景**：dashboard 数据已有，呈现散。

**实施**：
- `paid/dashboard.py` home 模板顶部加 2 个 metric 条：
  - `direct_rate_today` vs 50% target 进度条（绿/黄/红按 ≥50% / ≥30% / <30%）
  - `today_usd` vs `daily_soft_cap` / `daily_hard_cap` 双阈值条
- Recent Activity table 加 `question` 列（截断 80 字），位置在 topic 之后
- `collect_summary` 返回值已含 `direct_rate_today`，cost 字段从 `cost.cap_status()` 拉

**验收**：
- home 页顶部出现两条 metric 条
- Recent Activity table 多一列 q-preview
- 单测：`collect_summary` shape 不变（新字段不破坏 `/api/summary.json` 消费者）

**冲突评估**：🟢 零冲突

---

### A4 · M2.6.A review session 列表  🟢 P2

**背景**：review skill 自 v1.3.x 起在跑，dashboard 没接。

**实施**：
- `paid/dashboard.py` 加 `collect_review_sessions()` —— 扫 `~/.hermes/paid/cp/<cp_id>/review_sessions/`（实际路径以 review skill 落点为准）
- home 页加 "Active review sessions" section：列 session_id / cp / stage（subject/scan/qa/merge/gate/closed） / age / next-action
- 新加路由 `/reviews` 看完整列表
- 单 cp detail 页 (`/counterparties/<cp_id>`) 加 cp 的 review history 段

**验收**：
- 至少 1 个 active review session 时 home 页能看到
- closed session 在 `/reviews` 历史里
- 单测：`collect_review_sessions` 返回 shape 稳定

**冲突评估**：🟢 零冲突。先确认 review skill 数据落点（应在 `paid_review/`
skill 包内某固定路径，跟 cp profile 平行）

---

### A5 · M2.6.B 趋势 sparkline + cost 折线  🟡 P2

**背景**：master design §6 七项指标自评的天然 surface。

**实施**：
- `dashboard.py` 加 `collect_trend(days=7)` 和 `collect_trend(days=30)` —— 按日聚合 audit_log 的 direct/request/decline 计数
- 加 `collect_cost_trend(days=7)` —— 用 `cost.total_for_last_n_days` 已有的逐日 helper
- home 页或新 `/trends` view 渲染 SVG sparkline（纯 SVG，无 JS 依赖）
- 数据透传 `/api/trends.json`

**验收**：
- 7/30 天有 ≥3 天数据时 sparkline 正常绘制
- 单元测试覆盖按日聚合逻辑（mock audit_log）
- SVG 在 Safari + Chrome + Firefox 都正常显示

**冲突评估**：🟢 零冲突。audit_log + cost_ledger 都 ≥7 天数据（v1.5.0 release 起）

---

### A6 · M2.6.B 七项指标进度卡  🟡 P3

**背景**：master design §6 七项硬指标，目前没 surface 让 owner 看打勾几条。

**实施**：
- 新增 `paid/metrics_progress.py` —— 七项指标的 derived/manual 字段：
  1. ≥1 pilot 走完任务全周期：derived from cp count + closed task count
  2. 周报自动生成 ≥2 周：derived from `~/.hermes/paid/weekly_reports/`（M7.3 实装后）
  3. 跨组织 demo：manual flag in settings
  4. Twitter 长文 + demo 视频：manual flag in settings
  5. 5 个相关方深度私聊：manual count in settings
  6. README 给陌生人看：manual flag in settings
  7. 第 7 条按 master design 当前定义补
- dashboard 加 `/metrics-progress` view + home 页摘要框

**验收**：
- 七项卡片每项显示 ✓ / pending / 数字 + 来源（derived/manual）
- manual 字段在 settings.json 改后 reload 看见更新
- 七项中 ≥1 ✓ = home 页摘要框显示

**冲突评估**：🟢 零冲突。如果 master design §6 字段没全锁死，先做 5 项确定的
+ 2 项预留

---

### A7 · M2.6.B 跨平台聚合 / pilot 切分  📋 P4 / 推迟

**当前单 owner 单 pilot**，UI 没必要。先做 schema：
- `collect_summary` 加 `platform_breakdown` 字段（Lark / TG / Slack 各 cp 数 + 消息数）
- `collect_counterparties` 已经按 cp_id 全列，UI 端按 `cp.identities[].platform` group

UI 实施推迟到 multi-pilot onboard（M8.1 ≥3 pilot 之后）。

---

## 三、轨 B · hermes upstream PRs（并行）

### B1 · M5.2 `post_llm_call` 透传 sender_id  ✅ 最高 ROI

**实施**（hermes-agent 侧）：
- `run_agent.py:13783-13791` `post_llm_call` invoke 加 `sender_id=getattr(self, "_user_id", None) or ""`
- `hermes_cli/hooks.py:137-141` `_DEFAULT_PAYLOADS["post_llm_call"]` 加 `sender_id` 字段（同步给 `hermes hooks test` 用）

**实施**（paid-plugin 侧，跟 hermes merge 后）：
- 删除 `__init__.py:74-94` `_SESSION_META_CACHE` + `_cache_session_meta` + `_lookup_session_meta`
- 简化 `on_post_llm_call` `__init__.py:960-993` 直接读 kwargs，不 fallback cache
- `on_pre_llm_call` 也删 `_cache_session_meta(...)` 调用

**验收**：
- hermes PR：单测覆盖 `post_llm_call` kwargs 含 `sender_id`
- paid PR：进程重启后第一条 IM 也能正确 scope L4（之前 cache 丢会 scope 错）
- 跨进程一致：删 cache 后，cross-process 行为完全确定

**冲突评估**：🟢 hermes 那边纯加 kwarg，向后兼容；paid 侧 cleanup 干净

---

### B2 · M5.3 adapter `send()` 不硬编码 receive_id_type  ✅

**实施**（hermes-agent 侧）：
- `gateway/platforms/feishu.py:1694-1700` `send()` signature 加 `receive_id_type: str = "chat_id"` 默认值
- `_send_raw_message` (4057-4084) 透传 → `_build_create_message_request(receive_id_type, body)`
- `_build_create_message_request` 已经接受 receive_id_type 参数 (line 4378-4386)，零改动

**实施**（paid-plugin 侧）：
- `paid/hermes_io.py:471-779` 删 `_send_lark_direct` + `_send_lark_card_direct` 整套 ~300 行
- 改用 adapter `send(receive_id, content, receive_id_type=_detect_lark_receive_id_type(receive_id))`
- `_detect_lark_receive_id_type` helper 保留（前缀推断逻辑没人替代）

**验收**：
- 发到 `ou_*` open_id 不再走 PAID 自己的 HTTP，走 adapter
- send_telegram_card 等其他路径不受影响（feishu 独有改动）
- 跨 hermes 内部 refactor 时 PAID 不再因 adapter `_client` 接口变动炸

**冲突评估**：🟢 hermes 纯加 optional kwarg；paid 删 ~300 行 workaround

---

## 四、显式不做的（v1.5.5 不收）

| 项 | 不做理由 |
|---|---|
| M2.1 progressive disclosure | 等 pilot ≥3 人吐槽信息密度。JELabs 没反馈 |
| M2.2 `modified` 终态 | audit 现可 derive；等真有"想知道 owner 改了几次"产品需求 |
| M2.7 namespacing 统一 | breaking risk 大、功能影响零；JELabs pilot 已活，强制 migration 风险大 |
| M2.9 整模块 pre_tool_call | 致命前提错（详 backlog M2.9 重审），4 子项已重定向 |
| M5.1 chat_id 透传 | Lark `oc_/ou_/on_` 前缀推断 100% 可靠，省不下代码 |
| M3.5.A/B fork hermes / upstream PR for TG/Slack callback | v1.4.0 已用 M3.5.C lazy-hook 落地；够用，等多 pilot 痛 |

---

## 五、Ship 顺序建议

按 dependency + ROI 排：

1. **A1 doctor 命令** + **A2 cost ceiling inline** —— 都是独立小 PR，先 ship
   一对小 release（v1.5.5-rc1 或直接 v1.5.5），让 pilot 立刻受益
2. **A3 + A4** —— dashboard "今日 ops" polish，data 都已有，~1 个 PR ship 完
3. **A5 + A6** —— dashboard 宏观视图，~1 个 PR，含七项指标 schema 定型
4. **B1 + B2** —— hermes upstream PRs 同步起草、提 issue，等 merge 节奏

**预期 v1.5.5 ship 节奏**：
- 1.5.5-rc1（doctor + cost ceiling）：1-2 天
- 1.5.5-rc2（dashboard A 段）：2-3 天
- 1.5.5-rc3 → v1.5.5（dashboard B 段含七项指标卡）：3-5 天

如果太大可以拆成 v1.5.5（A1+A2+A3+A4）+ v1.5.6（A5+A6）。

---

## 六、跨仓 cross-ref

- backlog：`paid-may/design/05_backlog.md` M2.6 / M2.7 / M2.9 重审 / M5 / M9.4
- master design §6 七项硬指标：`paid-may/design/00_master_design.md`
- pilot 状态：`paid-may/outputs/pilot_tracker.md`（如有）
- hermes PR 草稿（待提）：暂存 paid-may `design/` 下另开文件

---

## 七、Owner action（不在本次实施）

读完此 plan 后决定：

1. **优先级是否调整**：A1 doctor 真的最高吗？还是 A2 cost ceiling 更急？
2. **A7 pilot 切分是否提前**：如果 v1.5.5 期间会 onboard ≥2 pilot，schema +
   UI 一起做
3. **B 段时机**：B1+B2 跟 A 段平行起草还是 A ship 完再做？
4. **七项指标卡（A6）的 7 项是否锁定**：master design §6 字段最近有改吗？
   如果还在动，A6 先做 5 项稳定的
