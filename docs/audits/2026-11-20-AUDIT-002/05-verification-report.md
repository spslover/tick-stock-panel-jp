# 05 验证报告（Verification Report）

所有以下结果均为本次在 `TARGET_ENV=local` 实际执行所得，未执行项明确标注 `NOT_EXECUTED` 或 `EXTERNAL_REQUIRED`。

## 1. 自动化测试与静态检查汇总

| 验证项 | 实际执行命令 | 结果 | 状态 |
|---|---|---|---|
| 后端关键静态缺陷门禁 | `cd backend; uv run --extra dev python -m ruff check --select F821,F811,RUF006 app/ tests/` | `All checks passed!`（0 errors） | `VERIFIED` |
| 后端本次改动文件全规则检查 | `cd backend; uv run --extra dev python -m ruff check app/services/observability.py app/api/routes.py app/api/auth.py app/api/config_backup.py tests/test_audit_observability_hardening.py` | `All checks passed!`（0 errors） | `VERIFIED` |
| 后端可观测性与审计脱敏回归 | `cd backend; uv run --extra dev python -m pytest tests/test_audit_observability_hardening.py -v` | **4 passed** (9.16s) | `VERIFIED` |
| 后端 Tier-1 E2E 可观测性/审计契约 | `cd backend; uv run --extra dev python -m pytest tests/e2e_prod/test_tier1_health_auth_gateway.py -m r3_post_fix -v --tb=short` | **5 passed** (34.37s) | `VERIFIED` |
| 后端开放契约/安全/加固/多语言/全球分析套件 | `cd backend; uv run --extra dev python -m pytest tests/test_openapi_contract.py tests/test_m1_edge_auth_security.py tests/test_m1_ssrf_secrets.py tests/test_m2_1_hardening.py tests/test_ui_prefs.py tests/test_global_market_data.py tests/test_global_analysis_api.py -q` | **166 passed**, 0 failed | `VERIFIED` |
| 前端 ESLint 错误门禁 | `cd frontend; corepack pnpm eslint --quiet .` | 退出码 0（**0 errors**，基线为 8 errors） | `VERIFIED` |
| 前端全量单元测试 | `cd frontend; corepack pnpm vitest run --maxWorkers=2` | **124 files passed, 844 tests passed** (295.80s) | `VERIFIED` |
| 前端生产构建 (TypeScript + Vite) | `cd frontend; corepack pnpm build` | `✓ built in 33.96s`（退出码 0） | `VERIFIED` |
| 全量 `e2e_prod` 含 SSE 长连接与外部回源 | `cd backend; uv run --extra dev python -m pytest tests/e2e_prod` | SSE 用例同步阻塞且行情回源离线返回 502（见 `TEST-E2E-ENV-001`） | `PARTIAL` |
| 离线依赖漏洞扫描 / Docker 构建 / 远端 CI | — | 受 `ALLOW_NETWORK=false` 与本地范围限制未执行 | `NOT_EXECUTED` |
| 第三方渗透测试与生产压测 | — | 需外部安全团队与 staging 环境 | `EXTERNAL_REQUIRED` |

## 2. 本地微基准与生命周期验证（非生产容量结论）

使用隔离临时目录与本地 `TestClient(app, client=("127.0.0.1", 50000))` 顺序执行 4 个运维端点各 50 次请求（合计 200 次）：

| 端点 | 样本数 | 错误数 | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| `GET /health/live` | 50 | 0 | 6.52 ms | 10.11 ms | 229.62 ms（含首次冷路径） |
| `GET /health/ready` | 50 | 0 | 5.63 ms | 9.99 ms | 10.99 ms |
| `GET /health` | 50 | 0 | 12.98 ms | 17.82 ms | 19.85 ms |
| `GET /metrics` | 50 | 0 | 9.47 ms | 14.67 ms | 15.11 ms |

- **进程常驻内存（RSS）**：`275.7 MB`
- **优雅停机检查**：`PullScheduler` / `FinancialScheduler` / `QuoteService` / `DepthService` / `WeComBotService` / `APScheduler` 全部正常退出，**0 次 `SchedulerNotRunningError`**。

> 说明：上述微基准仅验证本地 ASGI 栈健康度与新中间件开销，**不代表高并发生产容量或长期运行 SLO 结论**。
