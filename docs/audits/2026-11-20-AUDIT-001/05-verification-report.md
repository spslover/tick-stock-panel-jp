# 05 — 验证报告

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20
本报告只记录**实际执行过**的命令与环境。未执行的项一律标记 `NOT_EXECUTED`, 不以任何方式暗示其通过。

## 0. 环境前置条件

- 仓库根目录: `C:\Dev\AI\tick-stock-panel-jp`
- 后端解释器: `backend\.venv\Scripts\python.exe` (Python **3.12.13**, 与 CI 的 `uv python install 3.12` 一致)
- 前端工具: `frontend\node_modules\.bin\` 下的 tsc / vite / vitest / eslint (pnpm 不在 PATH, **未联网安装任何工具**)
- 网络: 未发起任何外部请求 (`ALLOW_NETWORK=false`)
- 未执行: 部署、git 提交/推送/合并、数据库迁移、生产访问、破坏性操作
- 停止条件: 任何命令若需网络或写生产数据, 立即停止并标记 NOT_EXECUTED

## 1. 静态验证

### 2.1 Ruff 基线 (V1)

- 命令: `cd backend; .venv\Scripts\ruff.exe check app tests --statistics`
- 环境: Windows 10.0.26300 x64, Python 3.12.13
- 结果: **退出码 1**。统计 (节选): 214 BLE001 (blind-except, 仓内已声明为可接受基线)、42 I001、37 RUF100、31 F401、16 B023 (函数使用循环变量)、14 E741、13 SIM102、12 E402、12 UP017、10 B905、10 F841、9 B008、8 N806、7 B904、7 RUF012、7 SIM108、6 RUF006、6 RUF059、5 E702、5 RUF023、5 SIM114、4 UP035、3 B017、3 N801、2 RUF046、2 UP041 …
- 失败判断: **这是审计前的既有基线**, 不是本次修改引入 —— 本次修改涉及的 3 个后端文件在修改后单独复查, 未引入新告警 (见 §3.5)。
- 处置: OPS-008 —— 存量过大, 不可直接转阻断门禁; 按 ADR-A4 先非阻断接入再 ratchet。
- 证据位置: 本节即证据; 原始计数可由上述命令复现。

### 2.2 ESLint 基线 (V2)

- 命令: `cd frontend; node_modules\.bin\eslint.CMD . --quiet`
- 结果: **退出码 1**, **8 errors / 227 warnings**。errors 明细:
  1. `frontend/scripts/i18n-audit.mjs:15` — 未使用变量 `BT`
  2. `frontend/scripts/i18n-reactive.mjs:80` — 未使用变量 `nearestFn`
  3. `frontend/src/pages/Watchlist.tsx:138` — 无用转义 \\-
  4-8. `frontend/src/pages/backtest/MiningWorkbench.tsx:134,160,175,180,220` — **rules-of-hooks: 在非组件函数内调用 `useI18n()`** (真实缺陷: 这些函数在渲染期被调用, 构成 hook 顺序隐患)
- 失败判断: 既有基线。因此 **eslint 未被接入阻断式门禁** (直接接入会使 CI 立刻变红)。
- 处置: OPS-008; 5 处 rules-of-hooks 建议优先修 (它们是真问题而非风格问题)。

### 2.3 前端测试基线 (V3)

- 命令: `cd frontend; node_modules\.bin\vitest.CMD run --reporter=basic`
- 结果: **Test Files 123 passed (123) / Tests 843 passed (843)**; Duration 204.63s (transform 491.34s, setup 0ms, collect 1817.47s, tests 118.90s, environment 193.16s, prepare 201.36s)
- 失败判断: 全绿 → 证明本次新增的 `pnpm test` 阻断门禁不会使 CI 变红 (OPS-001 修复的前提)。
- 证据位置: 本节; 该命令可原样复现。

### 2.4 类型检查与构建 (V4/V5)

- 命令: `cd frontend; node_modules\.bin\tsc.CMD -b` → **退出码 0**
- 命令: `cd frontend; node_modules\.bin\vite.CMD build` → **退出码 0**, built in 22.53s
- 产物体积 (实测): `index-*.js` **909.39 kB (gzip 479.86 kB)**、`echarts-*.js` **822.23 kB (gzip 270.42 kB)**、`zrender-*.js` 215.38 kB、`lightweight-charts-*.js` 186.94 kB、`Backtest-*.js` 188.85 kB、`Screener-*.js` 181.56 kB; vite 对 >500 kB chunk 给出告警
- 失败判断: 通过。体积告警对应 FE-007 / FE-003。
- 证据位置: 本节。

### 2.5 后端测试 (节选)

| 编号 | 命令 | 结果 |
| --- | --- | --- |
| V6 | `pytest tests/e2e_prod/test_tier1_health_auth_gateway.py -q` | **8 failed, 7 passed** (56.51s) —— 预先存在 |
| V7 | `pytest tests/test_config_backup.py -q` | **2 failed, 26 passed** (2.18s) —— 预先存在 (已做因果对照, §4) |
| V8 | `pytest tests/test_custom_pct_units.py tests/test_financial_shares.py tests/test_realtime_turnover_rate.py tests/test_quote_extra_null_columns.py tests/test_matrix_etf_share_fields.py tests/test_enriched_generation.py -q` | **1 failed, 67 passed** (202.87s); 失败项 `test_enriched_generation.py::test_heartbeat_renews_stale_marker` 预先存在 |
| N1 | `pytest tests -q` (完整套件) | **NOT_EXECUTED** —— 两次运行分别在约 45 分钟 (~4%) 与约 30 分钟仍未跑完 (瓶颈在 `tests/backtest/test_matrix_strategy.py` 等重型用例), 受本次后台任务 3600s 上限限制 |

> **诚实性说明**: 完整后端套件**没有**跑完。因此"本次修改未破坏任何既有测试"这一结论的覆盖面仅限已执行的这几组与本次改动直接相关的用例, **不代表全仓**。这是本审计最主要的验证缺口。

## 3. 功能与回归验证 (本次修改)

### 3.1 SEC-001 回归

- 命令: `cd backend; .venv\Scripts\python.exe -m pytest tests/test_analysis_menu_path_traversal.py -q --tb=short`
- **修复前**: **4 failed, 3 passed**。失败输出:
  ```text
  E   AssertionError: {"status":"deleted"}
  E   assert 200 == 400
  ```
  → 该响应体证明接口**真的执行了删除**; 测试同时断言 `secrets.json` 仍存在。
- **修复后**: `pytest tests/test_analysis_menu_path_traversal.py tests/test_analysis_menu_url_template.py -q` → **10 passed** (0.91s)
- 失败判断: 若任何用例失败, 视为修复无效, 不得放宽断言。
- 停止条件: 若发现同仓其他端点存在同类缺失, 停止并升级为批次 A6 的系统化审计。

### 3.2 DATA-001 回归

- 命令: `pytest tests/test_custom_pct_units.py -q` → **21 passed** (0.88s)
- 关键断言: `test_undeclared_change_pct_never_hundredfold_scaled` (百分制源不得出现 100 倍错值)、`test_undeclared_change_pct_recomputed_from_prices`、`test_undeclared_change_pct_null_without_prices`
- 失败判断: 任一失败即表示单位契约被破坏, 属 P0 回归。

### 3.3 DATA-002 回归

- 命令: `pytest tests/test_financial_shares.py -q` → **19 passed**
- 关键断言: `test_share_row_without_announce_date_is_not_available_at_period_end`、`test_share_row_without_announce_date_column_is_not_used_for_history`
- 失败判断: 任一失败表示历史股本报表期泄露仍在。

### 3.4 FE-01 回归

- 命令: `cd frontend; node_modules\.bin\vitest.CMD run src/pages/Indices.symbolSwitch.test.tsx src/pages/Indices.test.tsx --reporter=basic`
- **回退修复后**: **1 failed** — `AssertionError: expected '2026-09-09,2026-09-10' not to contain '2026-09-09'`
- **修复后**: **2 passed** (31.98s)
- 失败判断: 回退后必须失败, 否则测试无效 (无法捕获该缺陷)。

### 3.5 合并执行 (最终态)

```text
cd backend; .venv\Scripts\python.exe -m pytest \
  tests/test_analysis_menu_path_traversal.py \
  tests/test_analysis_menu_url_template.py \
  tests/test_custom_pct_units.py \
  tests/test_financial_shares.py -q
→ 40 passed (1.44s)
```

## 4. 预先存在失败的因果对照实验 (可复现)

目的: 证明 `test_config_backup.py` 的 2 项失败与本次修改无关。

```powershell
# 1) 取 HEAD 版本的 analysis.py (本次审计只改过这个文件)
git show HEAD:backend/app/api/analysis.py
# 2) 临时写回 HEAD 版本, 运行测试
cd backend
& .venv\Scripts\python.exe -m pytest tests/test_config_backup.py -q --tb=no -rf
#   → FAILED test_apply_into_fresh_env_roundtrip
#   → FAILED test_apply_conflict_default_skip_and_overwrite
#   → 2 failed, 26 passed
# 3) 立即恢复修复版本, 再跑一次
& .venv\Scripts\python.exe -m pytest tests/test_config_backup.py -q --tb=no -rf
#   → 2 failed, 26 passed  (完全相同)
```
**结论**: 两次结果一致 ⇒ 该失败为**预先存在**, 且 `test_config_backup.py` 不引用 `analysis`/`menu` (已 grep 确认) ⇒ 与本次修改无因果关系。
**失败判断**: 若步骤 2 与步骤 3 结果不同, 说明本次修改引入了回归, 必须回到台账定位。

## 5. 数据可靠性验证

- **迁移前后校验**: 本次修改**不含任何数据库/Parquet schema 变更**, 无 expand→migrate→validate→contract 链条需要验证 (DATA-002 的历史重算属后续批次 D 的工作, 本次未执行)。
- **事务回滚与失败恢复**: 未执行 (需故障注入环境) → NOT_EXECUTED。
- **关键业务不变量**: 通过 §3.2/§3.3 的新增用例验证 (单位契约、股本可用日)。
- **对账/重放/数据修复流程**: 已**静态**审阅并确认存在 (`data_integrity.scan_recent_integrity` → `integrity_check.run_check/run_repair` → `repair_daily.repair_integrity_issues` 暂存校验后落盘 → `daily_health`, 并精确失效 `repo.invalidate_enriched_asset`、`ScreenerService.clear_history_cache`、`strategy_cache.clear_cache`); **未实跑** → PARTIAL。

## 6. 性能验证

**本次未执行任何压测、基准或容量测试** (NOT_EXECUTED)。

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 负载模型 (LOAD_MODEL) | 空 | 任务书未提供, 且生产流量/峰值/数据量/读写比例/增长预测均未知 |
| CORE_SLA (吞吐/p95/p99/错误率) | 空 | 无目标即无法判定"是否达标" |
| 隔离环境压测 | NOT_EXECUTED | 需授权环境与停止条件 |
| 前后对比 | NOT_EXECUTED | 性能修复尚未实施 (PERF-001..004 均为 OPEN) |

唯一可报告的**实测**性能数据是构建产物体积 (§2.4), 属前端包体而非运行时性能。
审计报告中所有"慢"的表述 (如 PERF-001 每轮扫描的订单文件数) 均标注为**估算**, 依据是代码结构 (每订单一文件、从不清扫) 与代码注释中的量级, **不是测量值**。

## 7. 韧性验证

全部 **NOT_EXECUTED**: 依赖超时、进程退出、网络异常、消息重复、资源耗尽五类场景均未模拟。
原因: 需要可注入故障的隔离环境; 在单机本地执行会污染共享的 `data/` 与测试夹具, 且无法区分"被测缺陷"与"注入本身的问题"。
已交付替代品: `operations/` 下的演练脚本与预期结果 (§ 韧性矩阵), 可由具备条件的团队在授权环境执行。

## 8. 第三方与人工事项

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 第三方渗透测试 | **EXTERNAL_REQUIRED** | 未提供授权范围与资料。交接材料见 `security/third-party-test-handover.md` (含脱敏架构说明、授权范围模板、结果导入模板) |
| 内部评审 | **PENDING_HUMAN_SIGNOFF** | 模板见 `reviews/review-comments-template.md` 与 `reviews/signoff-template.md` |
| 签字确认 | **PENDING_HUMAN_SIGNOFF** | 同上; 本次**未伪造任何评审结论或签名** |
| 监管条款映射 | **NEEDS_COMPLIANCE_REVIEW** | JURISDICTION 与 REGULATORY_BASELINE 为空 |

## 9. 未执行事项汇总

| 编号 | 项目 | 状态 | 阻塞原因 |
| --- | --- | --- | --- |
| N1 | 完整后端测试套件 | NOT_EXECUTED | 运行时长超出本次后台任务上限 (见 §2.5) |
| N2 | 压测 / 基准 / 容量 | NOT_EXECUTED | 无 LOAD_MODEL 与 CORE_SLA; 禁止共享环境压测 |
| N3 | 故障注入 | NOT_EXECUTED | 需授权的隔离环境 |
| N4 | 备份恢复 / 容灾演练 | NOT_EXECUTED | 需第二环境与受控数据副本 |
| N5 | CVE 扫描 (pip-audit / pnpm audit / Trivy) | NOT_EXECUTED | 扫描器未安装, 不允许联网安装 |
| N6 | SAST / 镜像扫描 / SBOM | NOT_EXECUTED | 同上 |
| N7 | 第三方渗透测试 | EXTERNAL_REQUIRED | 未提供授权 |
| N8 | 99.99% 可用性验证 | NOT_EXECUTED | 需生产观测周期与已确认的 SLI/统计窗口/排除规则 |
| N9 | 真实供应商接口行为 | NOT_EXECUTED | 需有效 API Key, 属外部系统 |
| N10 | `data/` 运行时数据质量与规模 | NOT_EXECUTED | 用户运行时数据, 不在审计范围 |
| N11 | 内部评审与签字 | PENDING_HUMAN_SIGNOFF | 需人工执行 |
| N12 | 监管映射 | NEEDS_COMPLIANCE_REVIEW | 法域与规范未提供 |

## 10. 证据文件

| 文件 | 内容 |
| --- | --- |
| `evidence/pytest-baseline-partial.txt` | 完整套件两次运行的**部分**输出 (至约 4%), 证明其未完成而非全绿 |
| `evidence/pytest-baseline-verbose-partial.txt` | 第二次运行的逐用例明细 (定位重型用例所在文件) |
| `evidence/frontend-vitest-baseline.txt` | 前端 123 文件 / 843 用例全绿记录 |
| `evidence/frontend-build.txt` | tsc + vite 构建输出与产物体积 |
| `evidence/ruff-baseline.txt` / `evidence/eslint-baseline.txt` | 两套 lint 的基线统计 |

证据文件均已脱敏 (不含任何密钥、连接串或个人数据)。
