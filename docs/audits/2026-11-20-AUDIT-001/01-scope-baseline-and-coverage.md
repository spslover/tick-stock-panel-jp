# 01 — 范围、基线与覆盖矩阵

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 项目版本 v0.3.37 | 日期 2026-11-20

## 1. 运行输入与假设

本次任务书未提供具体值, 按任务书默认值执行, 并在此登记假设:

| 输入 | 取值 | 来源 |
| --- | --- | --- |
| PROJECT_ROOT | `C:\Dev\AI\tick-stock-panel-jp` | 当前工作目录 |
| EXECUTION_MODE | `audit_and_fix` | 任务书默认 |
| TARGET_ENV | `local` | 任务书默认 |
| AUTHORIZED_CHANGE_SCOPE | 仅本仓库 (`backend/`、`frontend/`、`docs/`、`.github/`) | 任务书默认 |
| BUSINESS_DOMAIN | A 股行情分析、策略选股、监控告警、回测与模拟盘 | 由代码与 docs/ 识别 |
| CORE_TRANSACTIONS | 实时行情 → enriched 指标 → 策略/监控 → 回测/模拟盘 (候选清单见 §5, **待业务确认**) | 由代码与 docs/ 识别 |
| JURISDICTION | **空** —— 未提供适用法域 | 任务书未提供 |
| REGULATORY_BASELINE | **空** —— 未提供适用规范 | 任务书未提供 |
| AVAILABILITY_TARGET | 99.99% 作为**设计目标**, 非已达成结论 | 任务书默认 |
| CORE_SLA / RTO_TARGET / RPO_TARGET / LOAD_MODEL | **空** | 任务书未提供 |
| ALLOW_NETWORK | `false` | 任务书默认 |
| ALLOW_DEPLOY / ALLOW_PRODUCTION_ACCESS / ALLOW_DESTRUCTIVE_TEST | `false` | 任务书默认 |
| EXTERNAL_SECURITY_TEST | 未提供 | → 状态 `EXTERNAL_REQUIRED` |
| OUTPUT_DIR | `docs/audits/2026-11-20-AUDIT-001` | 任务书默认 |

**因 JURISDICTION 与 REGULATORY_BASELINE 为空**, 本次审计只做通用工程控制审查, **未做也不得做**"符合某金融监管要求"的断言; 所有可能相关的映射一律标记 `NEEDS_COMPLIANCE_REVIEW`。

## 2. 环境与工具版本

| 项 | 实测值 | 备注 |
| --- | --- | --- |
| 操作系统 | Windows 10.0.26300 x64 (win32) | 主力桌面平台 |
| Python (系统) | 3.14.7 | 未用于本项目 |
| Python (项目 venv) | **3.12.13** (`backend/.venv`) | 与 CI 的 `uv python install 3.12` 一致 |
| uv | 0.11.4 | 锁文件安装 |
| Node | v24.21.0 | CI 用 Node 20 |
| corepack | 0.36.0 | |
| pnpm | **不在 PATH** | 改用 `frontend/node_modules/.bin/*` 直接执行 (vitest / eslint / tsc / vite), 不联网安装 |
| Ruff | 项目 venv 内 (ruff>=0.5) | |
| pytest | 项目 venv 内 (pytest>=8.0) | |
| Git | 2.53.0.windows.2 | |

## 3. 工作区状态 (必须先声明)

审计开始时工作区**已有大量未提交改动**, 且其中包含本次作者之外的他人改动:

- 已修改: 约 157 个路径, 绝大多数为 `frontend/src/**` (components / pages / lib / locales / router) 与 `docs/features.md`、`操作说明书.md`;
- 未跟踪: `frontend/scripts/i18n-display-check.mjs`、`i18n-display-usage.mjs`、`i18n-ja-scan.mjs`;
- 分支 `main`, HEAD `8cd1010`。

**处置**: 本次审计**未覆盖、未回滚、未格式化**任何既有改动; 所有修改均为增量, 涉及文件:
`backend/app/api/analysis.py`、`backend/app/data_providers/custom/provider.py`、`backend/app/share_capital.py`、`backend/tests/test_analysis_menu_path_traversal.py` (新增)、`backend/tests/test_custom_pct_units.py`、`backend/tests/test_financial_shares.py`、`frontend/src/pages/Indices.tsx`、`frontend/src/pages/Indices.symbolSwitch.test.tsx` (新增)、`docs/custom-data-source.md`、`.github/workflows/ci.yml`, 以及本审计交付目录。
**这些文件在审计开始时均未被修改** (后端无改动、前端 `Indices.tsx` 与 `Dashboard.tsx` 均不在既有改动清单中)。

## 4. 已执行的验证 (EXECUTED)

| # | 命令 | 环境 | 结果 | 证据 |
| --- | --- | --- | --- | --- |
| V1 | `backend/.venv/Scripts/ruff.exe check app tests --statistics` | 本地 Windows | **退出码 1**, 214 BLE001 / 42 I001 / 37 RUF100 / 31 F401 / 16 B023 / 14 E741 / 13 SIM102 / 12 E402 / 12 UP017 / 10 B905 / 10 F841 / 9 B008 / 8 N806 / 7 B904 / 7 RUF012 / 7 SIM108 / 6 RUF006 / 6 RUF059 / 5 E702 / 5 RUF023 / 5 SIM114 … | `05-verification-report.md` §2.1 |
| V2 | `frontend/node_modules/.bin/eslint.CMD . --quiet` | 本地 Windows | **退出码 1**, 8 errors / 227 warnings。errors: `scripts/i18n-audit.mjs:15` 未用变量 BT、`scripts/i18n-reactive.mjs:80` 未用变量 nearestFn、`pages/Watchlist.tsx:138` 无用转义 \\-、`pages/backtest/MiningWorkbench.tsx` 5 处 rules-of-hooks | 同上 §2.2 |
| V3 | `frontend/node_modules/.bin/vitest.CMD run --reporter=basic` | 本地 Windows | **通过**: **123 files / 843 tests passed**, 耗时 204.63s (transform 491.34s, collect 1817.47s) | 同上 §2.3 |
| V4 | `frontend/node_modules/.bin/tsc.CMD -b` | 本地 Windows | **退出码 0** | 同上 §2.4 |
| V5 | `frontend/node_modules/.bin/vite.CMD build` | 本地 Windows | **退出码 0**, built in 22.53s; `index-*.js` 909.39 kB (gzip 479.86 kB)、`echarts-*.js` 822.23 kB (gzip 270.42 kB)、vite 对 >500 kB chunk 告警 | 同上 §2.4 |
| V6 | `backend/.venv/Scripts/python.exe -m pytest tests/e2e_prod/test_tier1_health_auth_gateway.py -q` | 本地 Windows | **8 failed, 7 passed** (56.51s) —— 预先存在 | 同上 §2.5 |
| V7 | `… pytest tests/test_config_backup.py -q` | 本地 Windows | **2 failed, 26 passed** (2.18s) —— 预先存在 (见 §2.6 的对照实验) | 同上 §2.6 |
| V8 | `… pytest tests/test_custom_pct_units.py tests/test_financial_shares.py tests/test_realtime_turnover_rate.py tests/test_quote_extra_null_columns.py tests/test_matrix_etf_share_fields.py tests/test_enriched_generation.py -q` | 本地 Windows | **1 failed, 67 passed** (202.87s); 失败项 `test_enriched_generation.py::test_heartbeat_renews_stale_marker` 为预先存在 | 同上 §2.7 |
| V9 | `… pytest tests/test_analysis_menu_path_traversal.py` (修复前) | 本地 Windows | **4 failed, 3 passed** —— 复现漏洞: 响应体 `{"status":"deleted"}`, `secrets.json` 被真实删除 | 同上 §3.1 |
| V10 | `… pytest tests/test_analysis_menu_path_traversal.py tests/test_analysis_menu_url_template.py` (修复后) | 本地 Windows | **10 passed** (0.91s) | 同上 §3.1 |
| V11 | `… pytest tests/test_custom_pct_units.py` (修复后) | 本地 Windows | **21 passed** (0.88s) | 同上 §3.2 |
| V12 | `… pytest tests/test_financial_shares.py` (修复后) | 本地 Windows | **19 passed** | 同上 §3.3 |
| V13 | `vitest run src/pages/Indices.symbolSwitch.test.tsx src/pages/Indices.test.tsx` (修复后) | 本地 Windows | **2 passed** | 同上 §3.4 |
| V14 | `vitest run src/pages/Indices.symbolSwitch.test.tsx` (**回退修复后**) | 本地 Windows | **1 failed**: `expected '2026-09-09,2026-09-10' not to contain '2026-09-09'` —— 证明测试确实能捕获该缺陷 | 同上 §3.4 |

## 5. 未执行的验证 (NOT_EXECUTED)

| # | 项目 | 原因 | 状态 |
| --- | --- | --- | --- |
| N1 | **完整后端测试套件** `pytest tests -q` | 两次运行分别在 45 分钟 (~4%) 与 30 分钟时仍未跑完 (瓶颈在 `tests/backtest/test_matrix_strategy.py` 等重型用例); 本次审计的 3600s 后台任务上限不足以完成 | NOT_EXECUTED |
| N2 | 压测 / 性能基准 / 容量测试 | 任务书未提供 LOAD_MODEL 与 CORE_SLA; 且禁止在共享环境压测 | NOT_EXECUTED |
| N3 | 故障注入 (看门狗卡死、依赖超时、进程退出、网络异常、消息重复) | 需在授权环境进行; 本地单机不构成有效演练 | NOT_EXECUTED |
| N4 | 备份恢复 / 容灾演练 (跨可用区、跨区域) | 需要第二个环境与受控数据副本 | NOT_EXECUTED |
| N5 | CVE 扫描 (pip-audit / pnpm audit / Trivy / Grype) | 扫描器未安装且 ALLOW_NETWORK=false | NOT_EXECUTED |
| N6 | SAST / 容器镜像扫描 / SBOM 生成 | 同上 | NOT_EXECUTED |
| N7 | 第三方渗透测试 | 未提供授权范围与资料 | EXTERNAL_REQUIRED |
| N8 | 99.99% 可用性验证 | 需要生产观测周期与已确认的 SLI/统计窗口/排除规则 | NOT_EXECUTED |
| N9 | 真实供应商接口行为 (TickFlow / fuyao / tdxquant) | 需要有效 API Key, 且属于外部系统 | NOT_EXECUTED |
| N10 | `data/` 运行时数据的实际质量与规模 | 用户运行时数据, 不进入审计范围 | NOT_EXECUTED |
| N11 | 内部评审与签字 | 需人工执行 | PENDING_HUMAN_SIGNOFF |
| N12 | 监管条款映射 | JURISDICTION / REGULATORY_BASELINE 为空 | NEEDS_COMPLIANCE_REVIEW |

## 6. 覆盖矩阵

| 维度 | 范围 | 主要方法 | 覆盖度 | 关键产出 |
| --- | --- | --- | --- | --- |
| A 架构与业务适配 | 全仓模块边界、依赖方向、数据流 | AST 导入图 (309 模块) + 人工通读 | 广 (未逐行读 backtest/engine.py 168KB、matrix.py 162KB、strategy.py 136KB) | ARC-001..005 |
| B 代码质量与债务 | 全 backend + frontend/src | ruff / eslint 实跑 + 模式扫描 | 广 | QUAL-001..008 |
| C 安全与合规控制 | auth / api_gateway / secrets / 文件 I/O / 扩展信任边界 | 人工通读 + 路由级路径参数审计 + 漏洞复现 | 中 (未做动态渗透; 未审 api/strategy.py 全量、backend/app/plugins/*) | SEC-001..008 |
| D 性能与资源 | 行情热路径 / 缓存 / 并发 / 外部调用 | 代码路径分析 + 已有 benchmark 脚本清单 | 中 (**无实测压测**) | PERF-001..004 |
| E 数据一致性与事务 | 单位口径 / 复权 / 交易日 / 股本 / 回测未来函数 | 沿 CONTRIBUTING §3 逐条对照实现 | 中高 | DATA-001..006 |
| F 高可用与连续性 | 单点 / 探针 / 优雅停机 / 备份 | 人工通读 | 中 | HA-001..005 |
| G 运维与发布 | CI/CD / 可观测性 / 告警 / 文档 | workflow 逐行核对 + 文档路径交叉验证 | 广 | OPS-001..014 |
| H 测试与业务风险 | 479 后端 + 123 前端测试文件 | 模式统计 + 实跑 3 组 | 广 (全量未跑完) | TEST-001..006 |
| 前端专项 | api.ts / queryKeys / SSE / 状态 / 扩展边界 / 包体 | 人工通读 + 构建实测 | 中高 | FE-001..009 |

## 7. 业务能力 → 代码 → 测试 覆盖矩阵 (H 维度)

| 业务能力 | 代码入口 | 测试 | 判定 |
| --- | --- | --- | --- |
| 实时行情 | `services/quote_service.py`, `api/kline.py` | test_quote_chain / test_quote_freshness / test_quote_index_merge / test_realtime_turnover_rate / test_realtime_enriched_resume 等 | COVERED (含单位语义断言) |
| 日线管道与同步 | `jobs/daily_pipeline.py`, `services/kline_sync.py` | test_daily_pipeline_* / test_kline_sync_* / test_adj_factor_standalone_sync 等 | COVERED (15:35 调度本身未测) |
| enriched 指标 | `indicators/pipeline.py`, `enriched_generation.py` | test_enriched_* (9 个) / test_indicator_needed / test_live_enriched_*_parity | COVERED (最强区域) |
| 策略执行 | `strategy/engine.py`, `strategy/registry.py` | test_strategy_registry / _scoring / _cache / _run_all_parallel / _safety_validation 等 | COVERED; "不可计算"评分态缺专测 |
| 监控规则与通知 | `strategy/monitor.py`, `api/monitor_rules.py`, `services/notify_adapter.py` | test_monitor_* (8) / test_intraday_monitor_signals / test_notification_adapters / test_webhook_test | COVERED (规则/去重/文案); 真实飞书/企微投递仅 mock |
| 回测与步进优化 | `backtest/engine.py`, `matrix.py`, `optimizer.py` | test_walkforward / test_strategy_backtest_correctness / test_minute_fill / test_limit_up_close_fill / test_cost_model 等 | COVERED (信号-成交分离/T+1/涨跌停不可成交) |
| 自选股 | `api/watchlist.py` | test_watchlist_csv / _groups / _enriched_join / _ocr / _prepare | COVERED |
| 模拟盘 | `strategy/paper.py`, `api/paper.py` | 13 个文件 (test_paper_*) | COVERED |
| 认证与网关 | `api/auth.py`, `services/auth.py`, `services/api_gateway.py` | test_m1_edge_auth_security / test_api_gateway / test_openapi_contract | COVERED (含冻结的 Tier-A openapi 快照 —— 仓内最强门禁) |
| 能力路由 | `data_providers/capabilities.py` | test_capability_matrix / test_capability_augment / test_custom_pct_units | COVERED |
| 数据完整性与修复 | `services/integrity_check.py`, `api/data.py` | test_data_integrity / _api / _symbol_repair / test_precise_daily_repair 等 | COVERED |
| 自定义数据源插件 | `data_providers/custom/`, `plugins/` | test_custom_loader_install / _depth_provider / _financial_provider / _batch_isolation / test_m1_ssrf_secrets | COVERED (隔离/缺能力 fail-closed) |
| **MCP 对外接口** | `mcp-server/src/tsp_mcp_server/server.py` | 仅 `smoke_test.py` (手工脚本, 需活服务+Token) | **NOT COVERED** (且不在 CI) → OPS-013 |
| **生产可观测性契约** | `watchdog.py`, `logfile.py`, `api/routes.py` | 单测有; e2e_prod 断言的四个端点**不存在** | **NOT COVERED** → OPS-002 / TEST-001 |

## 8. 工具与版本登记

| 工具 | 版本 | 用途 | 是否产出可复现证据 |
| --- | --- | --- | --- |
| ruff | 仓内 (>=0.5) | 静态检查基线 | 是 (V1) |
| eslint | 仓内 9.39.5 | 前端静态检查基线 | 是 (V2) |
| vitest | 仓内 3.2.4 | 前端测试基线与回归 | 是 (V3, V13, V14) |
| tsc | 仓内 5.5.4+ | 类型检查与构建 | 是 (V4) |
| vite | 仓内 5.4.3+ | 产物构建与包体实测 | 是 (V5) |
| pytest | 仓内 (>=8.0) | 后端测试 | 部分 (V6..V12) |
| pip-audit / trivy / grype / syft | **未安装** | 依赖与镜像扫描 | 否 → NOT_EXECUTED (N5/N6) |

未安装任何新工具, 未发起任何网络请求, 未执行任何部署、提交、推送、合并或破坏性操作。
## 9. 维度成熟度评分

### 9.1 评分方法 (公开)

成熟度 0-5 级: 0 缺失或完全失控; 1 零散、依赖个人、基本不可验证; 2 部分实现但覆盖或可靠性明显不足; 3 主要机制已实现但仍有重要缺口或验证不足; 4 实现完整、具备自动化度量和持续验证; 5 经多场景实测与演练、证据完整。

**维度得分 = 成熟度 ÷ 5 × 100**。总分 = 各维度得分的**等权平均** (8 个维度各 1/8)。
权重与公式在此公开; **总分仅用于趋势比较, 不得抵消未解决的严重安全问题、数据丢失风险或核心链路单点故障** —— 门禁以 `06-residual-risk-and-release-decision.md` 的逐条判定为准。

### 9.2 评分结果

| 维度 | 成熟度 | 得分 | 评分依据 | 证据完整度 | 结论置信度 | 该维度问题数 |
| --- | --- | --- | --- | --- | --- | --- |
| A 架构与业务适配 | 3 | 60 | 模块边界与数据流清晰, 价格/时间/单位契约有单一权威实现; 但存在 API 层持有存储布局 (ARC-002)、两套 provider 入口 (ARC-004)、实时路径复用按需慢路径 (ARC-001) | 高 | 中高 | 5 |
| B 代码质量与技术债务 | 2 | 40 | 注释与命名规范良好, 测试密度高; 但无 lint 门禁 (实测 ruff 约 700 条、eslint 8 errors/227 warnings), 存在 3 个上帝模块 (preferences 139 函数 / api.ts 5133 行 / 回测页 3206 行) | 高 (两套 lint 均实跑) | 高 | 8 |
| C 安全与合规控制 | 3 | 60 | 边界控制扎实 (SSRF 防护、归档白名单、AST 校验、令牌只存摘要、CSRF 同源校验、CSP); 但路径校验在各端点不统一 (SEC-001, 已复现)、无安全审计留痕 (SEC-002)、无任何 CVE 扫描 (OPS-004) | 高 (含漏洞复现与修复验证) | 高 | 8 |
| D 性能与资源利用 | 2 | 40 | 缓存分层、准入控制、重任务限流、内存预算、并发去重都已在位; 但实时线程每轮做全量订单扫描并持锁 (PERF-001)、缓存未命中时全量扫 Parquet (ARC-001)、市场总览约 25 遍逐行聚合 (PERF-002), 且**无任何实测基线** | 中 (纯代码路径分析, 未压测) | 中 | 4 |
| E 数据一致性与事务 | 3 | 60 | 涨跌停/复权/北京时间/未来函数防护做得扎实 (回测信号-成交分离、T+1、涨跌停不可成交、基本面按公告日生效), 写入走原子替换与代际标记; 但存在两处可复现的口径违背 (DATA-001 单位启发式、DATA-002 股本报表期泄露) 与两处口径不一致 (DATA-003/004) | 高 (含复现、回归与因果对照) | 高 | 6 |
| F 高可用与业务连续性 | 2 | 40 | 有看门狗、僵死自愈、挖掘锁降级、优雅停机顺序、断点续跑; 但**市场数据无备份** (HA-001)、桌面版看门狗硬杀 (HA-002)、无 readiness (HA-004)、同目录双实例可并发写 (HA-003) | 中高 | 中高 | 5 |
| G 运维、可观测性与发布 | 2 | 40 | 有日志轮转、工作负载计数、NDJSON 心跳范式、三平台 CI 与发布链路; 但**前端测试从不进 CI** (OPS-001)、**CI 后端门禁当前为红** (OPS-002)、无指标出口/无告警/无关联 ID (OPS-009)、镜像发布不依赖 CI (OPS-003)、无供应链扫描 (OPS-004) | 高 | 高 | 14 |
| H 测试与业务风险 | 3 | 60 | 479 后端 + 124 前端测试文件, 核心不变量 (T+1、涨跌停不可成交、单位、原子写、代际) 有专项覆盖, 能力矩阵与 Tier-A 契约有冻结快照; 但断言强度普遍停留在 HTTP 200 (415 处)、依赖真实墙钟 (88 处)、sleep 同步 (62 处)、Windows 覆盖只有 11 个指定文件 | 中 (完整后端套件未跑完) | 中高 | 6 |
| **加权总分** | — | **50.0 / 100** | 等权平均 (60+40+60+40+60+40+40+60)/8 | — | — | **65** |

### 9.3 不适用项及理由

| 项 | 判定 | 理由 |
| --- | --- | --- |
| 监管合规映射 | **N/A / NEEDS_COMPLIANCE_REVIEW** | JURISDICTION 与 REGULATORY_BASELINE 未提供, 无法确定适用条款; 仅完成通用工程控制审查, **不作任何合规断言** |
| 跨可用区 / 跨区域容灾 | **N/A (形态层面)** | 产品为单实例自托管形态, 当前部署拓扑中不存在跨 AZ 组件; 已改以"本地数据快照 + 桌面版自愈"为对应目标 (批次 C1/C2) |
| Kubernetes / IaC 扫描 | **N/A** | 仓内无 K8s 清单或 IaC 代码; 部署形态为 Docker Compose 与 PyInstaller 桌面版 |
| MCP server 的自动化覆盖 | **N/A (现状)** | 该组件无 pytest 套件, 仅手工 smoke_test; 已作为 OPS-013 登记, 非"不适用"而是"缺覆盖" |

### 9.4 分数的解释边界

- 50.0 **不是**质量评分, 也**不代表**项目有 50% 的问题。
- 多数维度的"2 分"并非机制缺失, 而是"机制存在但没有门禁与证据把它钉住" —— 这正是本审计 65 条发现的主线。
- **门禁不参考总分**: 即使总分上升, `NO_GO` 条件 (未处置 P0、可复现的数据破坏或权限绕过、核心回归失败、引入新严重漏洞、不可回滚) 任一命中即不得发布。