# 06 残余风险与发布门禁结论（Residual Risk & Release Decision）

## 1. 发布门禁结论

- **受控内网 / 单节点试运行场景**：**`CONDITIONAL_GO`**
- **面向公网多用户 / 99.99% 金融级高可用生产场景**：**`NO_GO`**

> 依据原则：本次在本地环境关闭了全部已确认的 P0 可观测性/审计缺口与 P1 运行时崩溃/停机异常/悬空任务问题；但系统仍为单节点本地存储架构，且未完成第三方渗透测试、离线外网 SCA 扫描、真实容量压测、灾备恢复计时演练及人工签字，因此不得宣称无条件 `GO` 或“已通过金融级合规认证”。

---

## 2. 残余风险清单

| 风险项 | 关联 ID | 严重级别 | 当前状态 | 缓解措施与放行前必须条件 |
|---|---|---|---|---|
| 单节点本地存储架构无多活容灾 | `ARC-SINGLE-NODE-001` | High | `BLOCKED` | 内网单节点部署需配置每日定时快照备份（见 [`operations/backup-restore-and-incident.md`](file:///c:/Dev/AI/tick-stock-panel-jp/docs/audits/2026-11-20-AUDIT-002/operations/backup-restore-and-incident.md)）；若需 `99.99%` SLA，必须先完成状态外置与双活改造 |
| 容器以 root 运行且默认绑定 `0.0.0.0:3018` | `OPS-CONTAINER-001` | High | `PARTIAL` | 生产部署前须在 `.env` 设置 `HOST=127.0.0.1` 并置于启用 TLS/WAF 的反向代理后；协调宿主机 `./data` 卷属主后切换非 root `USER` |
| 第三方渗透测试与外部漏洞/供应链扫描未执行 | — | High | `EXTERNAL_REQUIRED` | 上线公网前由独立安全团队依据 [`security/third-party-test-handover.md`](file:///c:/Dev/AI/tick-stock-panel-jp/docs/audits/2026-11-20-AUDIT-002/security/third-party-test-handover.md) 执行渗透测试，并在有网环境运行 `pip-audit` / `pnpm audit` / `trivy` |
| 适用法域与金融监管条款未确认 | — | Medium | `NEEDS_COMPLIANCE_REVIEW` | 由法务/合规团队明确适用法域与监管要求，完成控制项映射与留存周期确认（如 `audit.jsonl` 防篡改归档与保留年限） |
| 真实流量压测、混沌演练与远端 CI/镜像构建未执行 | `OPS-CI-001`, `TEST-E2E-ENV-001` | Medium | `NOT_EXECUTED` | 在 staging 环境完成目标 QPS 压测、看门狗重启恢复演练、Parquet 备份恢复演练及 GitHub Actions 全绿验证 |
| 正式人工审批签字待完成 | — | Medium | `PENDING_HUMAN_SIGNOFF` | 架构、安全、SRE 与业务负责人审阅本审计包并签署 [`reviews/signoff-template.md`](file:///c:/Dev/AI/tick-stock-panel-jp/docs/audits/2026-11-20-AUDIT-002/reviews/signoff-template.md) |
