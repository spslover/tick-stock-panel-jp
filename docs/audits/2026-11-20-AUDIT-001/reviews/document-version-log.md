# 评审 — 文档版本记录

审计编号 AUDIT-2026-11-20-001

## 文档清单与版本

| 文件 | 版本 | 状态 | 最后修改 | 负责人 |
| --- | --- | --- | --- | --- |
| `00-executive-summary.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `01-scope-baseline-and-coverage.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `02-architecture-review.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `findings.json` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `03-remediation-plan.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `04-remediation-ledger.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `05-verification-report.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `06-residual-risk-and-release-decision.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `security/threat-model.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `security/third-party-test-handover.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `operations/sli-slo-and-alerts.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `operations/runbook-core-links.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `operations/backup-restore-and-incident.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `reviews/document-version-log.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `reviews/review-comments-template.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `reviews/signoff-template.md` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |
| `evidence/` | v1.0 | 待评审 | 2026-11-20 | 审计负责人 |

## 审计对象版本登记

| 项 | 值 |
| --- | --- |
| 项目 | tick-stock-panel-jp |
| 版本 | v0.3.37 (`VERSION`, `backend/pyproject.toml`, `frontend/package.json`, `backend/app/__init__.py` 一致) |
| 代码版本 | `8cd101035b1cd3cf995a3548de51a9b87c927514` |
| 分支 | `main` |
| 审计前工作区状态 | **非干净**: 约 157 个路径含未提交改动 (绝大多数为前端), 另有 3 个未跟踪脚本 |
| 审计产生的变更 | 8 个文件修改 + 2 个文件新增, 详见 `04-remediation-ledger.md` |
| 审计编号 | AUDIT-2026-11-20-001 |

## 变更历史

| 版本 | 日期 | 变更 | 作者 |
| --- | --- | --- | --- |
| v1.0 | 2026-11-20 | 首次发布 (阶段 0-7 全部交付物) | 审计负责人 |

## 后续修订约定

- 新增发现 → `findings.json` 追加条目, 不改动既有条目的 `status`/`severity` 语义;
- 修复状态变化 → `04-remediation-ledger.md` 增加对应小节, 并在 `findings.json` 的 `remediation_status` 同步;
- 评审意见 → `reviews/review-comments-template.md` 记录, 并在本文档追加版本行;
- 任何状态必须取自: IMPLEMENTED、VERIFIED、PARTIAL、NOT_EXECUTED、BLOCKED、EXTERNAL_REQUIRED、PENDING_HUMAN_SIGNOFF、NEEDS_COMPLIANCE_REVIEW。
