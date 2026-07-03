# CLAUDE.md — paid-plugin

## 仓库与部署纪律（全局真源：Mac `~/Desktop/vps-ops/REPO_MANAGEMENT.md`）
- main 不直推：PR + squash merge，合并后立即删本地分支；并行开发的第二克隆（如 paid-plugin-rich-text）分支合并后删除整个目录。
- **部署现状**：VPS `/home/paid/src/paid-plugin` 为手工同步（PR #43 的 GH Actions 部署未合前）。同步 pilot 前先确认 jelabs 影响面；send_dm 直发不产生 gateway log ≠ 没发出。
- hermes-agent 上游改动 = idempotent patch 脚本进本 repo，勿直接改 VPS 上的 clone。
- 凭据（Lark App Secret 等）永不写入代码/文档；真值只存 VPS 对应用户配置。
