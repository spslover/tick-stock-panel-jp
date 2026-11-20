# 03 修复方案与实施计划（Remediation Plan）

## 1. 优先级与分批实施策略

遵循最小差异原则，优先在本地可验证范围内关闭 P0/P1 缺陷，不进行破坏性重写或无关依赖升级：

| 批次 | 关联问题 ID | 风险等级 | 实施内容 | 预计工作量（角色/人日/置信度） | 执行状态 |
|---|---|---|---|---|---|
| Batch 1 | `OPS-OBS-001`, `REL-SHUTDOWN-001` | P0 / P1 | 新增 `observability.py`；落地 `/health/live`、`/health/ready`、`/metrics`、`X-Request-ID`、脱敏 `audit.jsonl`；修复 `scheduler.shutdown()` 异常 | 后端/SRE 0.5–1.0 人日（高置信度） | `VERIFIED` |
| Batch 2 | `FE-HOOK-001`, `QUAL-LINT-001` | P1 / P2 | 修复 `MiningWorkbench.tsx` 5 处 Hook 违规及剩余 3 处 ESLint 错误 | 前端 0.25–0.5 人日（高置信度） | `VERIFIED` |
| Batch 3 | `REL-TASK-001`, `QUAL-BUG-001`, `TEST-M2-001` | P1 / P2 | 替换 6 处裸 `asyncio.create_task`；修复 `F821`/`F811`；对齐 `test_m2_1_hardening.py` 契约并新增 `test_audit_observability_hardening.py` | 后端/QA 0.5–1.0 人日（高置信度） | `VERIFIED` |
| Batch 4 | `OPS-CONTAINER-001`, `OPS-CI-001` | P1 | 为 `Dockerfile` 与 `docker-compose.yml` 添加 `HEALTHCHECK`；在 `ci.yml` 接入 `ruff` 关键规则与 `eslint` 门禁 | SRE/DevOps 0.25–0.5 人日（高置信度） | `PARTIAL`（待远端 CI 与镜像构建实测） |
| Batch 5（后续迭代） | `OPS-CONTAINER-001`（非 root/反向代理）, `OPS-CI-001`（docker 门控与 Trivy）, `TEST-E2E-ENV-001` | P1 / P2 | 协调宿主机卷权限切非 root 容器；启用 `docker.yml` CI 门控 + SBOM/SCA；重构 SSE 异步测试桩 | SRE/QA 2–4 人日（中置信度） | `NOT_EXECUTED` |
| Batch 6（架构演进） | `ARC-SINGLE-NODE-001` | P1（高可用目标前提） | 状态外置（PostgreSQL + Redis）、多实例无状态部署、跨区灾备与自动化 RTO/RPO 演练 | 架构/后端/SRE 15–30 人日（中低置信度，取决于业务 SLA） | `BLOCKED` |

## 2. 回滚原则
- Batch 1–4 均为局部文件级修改，未改变任何现有持久化 Schema 或公开 API 响应契约，可通过单文件 `git checkout` / PR revert 在分钟级无损回滚。
