# 01 审计范围、基线与覆盖边界

## 1. 系统识别与技术栈

| 维度 | 现状识别（基于代码与配置文件） |
|---|---|
| 应用定位 | 股票/ETF 量化行情面板、因子/策略筛选、回测挖掘、模拟盘交易与多市场（A股/日股/美股）AI 分析平台 |
| 后端技术栈 | Python 3.12 (`pyproject.toml` 要求 `>=3.11`)、FastAPI + Uvicorn、DuckDB + Polars + PyArrow（本地 Parquet 湖仓）、SQLite（模拟盘执行租约/任务状态）、APScheduler |
| 前端技术栈 | React 18 + TypeScript 5.5 + Vite 5 + TanStack Query + TailwindCSS + ECharts，支持 `zh-CN` / `en` / `ja` 三语 |
| 认证与网关 | 会话密码认证（scrypt 哈希 + HttpOnly/SameSite=Lax Cookie + 登录 IP 限流 + CSRF 同源校验）+ Open API Bearer Token（SHA-256 哈希存储、6 级 scope、令牌桶限流、默认仅本机/内网） |
| 扩展与二次开发 | 后端 `app/custom/` 与前端 `src/custom/` 扩展注册表（`docs/secondary-development.md`） |
| 部署拓扑 | 单容器 Docker (`Dockerfile` + `docker-compose.yml`) 或桌面/本机进程（`HOST=0.0.0.0:3018`），无外部消息中间件与独立数据库 |

## 2. 审计范围清单

### 已纳入范围
- 后端核心入口、中间件、认证、Open API 网关、数据管道、模拟盘、看门狗、可观测性（`backend/app/**`）
- 前端核心页面、国际化机制、构建配置与静态检查（`frontend/src/**`, `frontend/scripts/**`）
- 容器与 CI/CD 定义（`Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, `.github/workflows/docker.yml`）
- 自动化测试套件（`backend/tests/**`, `frontend/src/**/*.test.*`）

### 未提供或无法访问（明确排除）
- 生产环境主机、真实行情 API 凭据、生产流量日志与监控历史（`ALLOW_PRODUCTION_ACCESS=false`）
- 明确适用的国家/地区金融监管条款清单与内部合规制度（`JURISDICTION` / `REGULATORY_BASELINE` 未提供）
- 第三方渗透测试报告、商业 SAST/DAST/SCA 扫描服务（`ALLOW_NETWORK=false`, `EXTERNAL_SECURITY_TEST` 未提供）
- 生产容量模型、峰值 QPS、RTO/RPO 正式业务指标（`LOAD_MODEL` / `CORE_SLA` / `RTO_TARGET` / `RPO_TARGET` 未提供）

## 3. 修改前基线（Stage 0 实测记录）

| 检查项 | 执行命令 | 基线结果 |
|---|---|---|
| 后端开放契约测试 | `cd backend; uv run --extra dev python -m pytest tests/test_openapi_contract.py -q` | 1 passed (7.75s) |
| 后端安全/加固/多语言/全球分析套件 | `cd backend; uv run --extra dev python -m pytest tests/test_m1_edge_auth_security.py tests/test_m1_ssrf_secrets.py tests/test_m2_1_hardening.py tests/test_ui_prefs.py tests/test_global_market_data.py tests/test_global_analysis_api.py -q` | 163 passed, **2 failed** (`test_m2_1_hardening.py`: L300, L459) |
| 后端 E2E 健康/认证/网关套件 | `cd backend; uv run --extra dev python -m pytest tests/e2e_prod/test_tier1_health_auth_gateway.py -v --tb=short` | 7 passed, **8 failed**（含 5 个 `r3_post_fix` 缺失端点/日志 + `SchedulerNotRunningError` 停机异常） |
| 后端静态分析 (ruff) | `cd backend; uv run --extra dev python -m ruff check --statistics app/ tests/` | 584 total issues，含 **1 `F821`**, **1 `F811`**, **6 `RUF006`** |
| 前端静态分析 (eslint) | `cd frontend; corepack pnpm eslint --quiet .` | **8 errors**（5 个 `MiningWorkbench.tsx` Hook 违规 + 3 个语法/未使用变量） |
| 前端构建 | `cd frontend; corepack pnpm build` | Passed (`58.21s`)；`index-*.js` (909 kB) 与 `echarts-*.js` (822 kB) 超过 500 kB chunk 告警 |
| 离线依赖漏洞扫描 (pip-audit / pnpm audit) | — | **`NOT_EXECUTED`**（`ALLOW_NETWORK=false`） |
| 容器镜像构建与 Trivy 扫描 | — | **`NOT_EXECUTED`**（本地未执行镜像构建且无网络） |
