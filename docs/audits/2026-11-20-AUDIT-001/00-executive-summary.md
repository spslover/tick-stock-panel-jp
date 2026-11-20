# 00 — 执行摘要 (Executive Summary)

- 审计编号: AUDIT-2026-11-20-001
- 项目: tick-stock-panel-jp (Tick Stock Panel) v0.3.37
- 审计代码版本: `8cd101035b1cd3cf995a3548de51a9b87c927514` (branch `main`)
- 审计日期: 2026-11-20 (JST)
- 执行模式: `audit_and_fix` | 目标环境: `local` | 授权变更范围: 仅本仓库
- 交付目录: `docs/audits/2026-11-20-AUDIT-001/`

## 1. 审计范围

| 项目 | 内容 |
| --- | --- |
| 后端 | `backend/app` 310 个 Python 模块 (api / services / tickflow / data_providers / plugins / indicators / strategy / backtest / custom / extensions / factors / jobs) |
| 前端 | `frontend/src` 471 个 ts/tsx 文件 (components / pages / lib / custom / extensions / locales) |
| 测试 | `backend/tests` 479 个文件 (其中 `test_*.py` 448) + `frontend/src` 123 个 `*.test.ts(x)` |
| 供应链与发布 | `backend/pyproject.toml` + `uv.lock`、`frontend/package.json` + `pnpm-lock.yaml`、`mcp-server/`、`.github/workflows/*`、`Dockerfile`、`docker-compose.yml` |
| 规范基线 | `CONTRIBUTING.md`、`docs/secondary-development.md`、`docs/custom-data-source.md`、`docs/plugin-development.md` |

**未纳入范围 (不构成审计结论)**: 生产环境与生产数据、`data/` 用户运行时数据、真实供应商 (TickFlow / fuyao / tdxquant) 的线上接口行为、第三方渗透测试、跨可用区与跨区域演练、组织级 GitHub 仓库设置 (Dependabot / 分支保护 / secret scanning)。

## 2. 总体结论

本项目是一个**工程质量高于平均水平的自托管金融面板**: 数据契约 (§3)、插件边界 (§4)、缓存一致性 (§6)、回测未来函数防护 (§5.3) 都有成文规则并在代码中大体落实; 边界安全 (SSRF 防护、归档解包白名单、AST 校验、令牌只存摘要) 做得扎实。审计发现的多数问题不是"没做", 而是**规则与实现脱节**、**门禁从未真正接上**、以及**个别路径缺了同一份校验**。

- 共登记 **65 项问题**: High 13 / Medium 32 / Low 20。
- 5 项 P0/P1 已在本次审计中修复并附回归测试; 其余 60 项为 OPEN, 均已给出修复方案、验证方法与回滚方式。
- **发布建议: CONDITIONAL_GO** —— 理由见 `06-residual-risk-and-release-decision.md`。阻断项已消除, 但仓库自带的 CI 后端门禁本身为红 (8 项预先存在的失败), 且未做第三方渗透测试、长期可用性未验证。

## 2.1 维度成熟度评分 (等权, 方法见 `01-scope-baseline-and-coverage.md` §9)

| 维度 | A 架构 | B 质量 | C 安全 | D 性能 | E 数据 | F 可用性 | G 运维 | H 测试 | 总分 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 得分 | 60 | 40 | 60 | 40 | 60 | 40 | 40 | 60 | **50.0 / 100** |

多数维度的低分并非"机制缺失", 而是**机制存在却没有门禁与证据把它钉住** —— 这正是 65 条发现的主线。
**总分仅用于趋势比较, 不参与发布门禁**; 门禁以 `06-residual-risk-and-release-decision.md` 的逐条判定为准。

## 3. 本次修复的重大问题 (IMPLEMENTED + VERIFIED)

### SEC-001 / P0 — 分析菜单删除接口路径穿越, 可删除任意 .json (已复现)
`DELETE /api/analysis-menus/{menu_id}` 未校验 `menu_id` 也未做目录包含检查, 直接 `unlink()`。
**审计中实际复现**: 请求 `/api/analysis-menus/..%5Cuser_data%5Csecrets` 返回 `{"status":"deleted"}`, `data/user_data/secrets.json` 被真实删除 —— 可连带删除 `auth.json`(会话与密码哈希) 与 `api_tokens.json`(全部 API Token), 删除后面板回到未初始化状态, 任意内网主机可重新设密接管。
根因是**写入侧有校验、删除侧没有**。修复: 提取共享的 `_validate_menu_id` 并在 `_path()` 内同时做标识符白名单与 `p.parent == base.resolve()` 包含检查。

### DATA-001 / P0 — 自定义实时源用截面数值猜涨跌幅单位 (违反 §3.1)
未声明 `pct_unit` 时, 代码按 `|change_pct|` 中位数(样本<5 用最大值)是否超过 0.31 判定整批是否 /100。百分制源在平静行情或小样本批次下会被当成小数制, 产生**静默的 100 倍错误**, 污染 enriched、监控阈值、涨跌停判定与展示。
修复: **取消一切按数值的推断**。`change_pct` 改由 `(last_price - prev_close) / prev_close` 重算 —— 价格比值与源端单位无关, 恒为小数制; 缺价格时置 `None` (与 amplitude/turnover_rate 同为 fail-closed)。文档同步更正。

### DATA-002 / P1 — 历史股本在公告日缺失时按报表期生效 (违反 §3.4 报表期泄露)
`announce_date` 为空的财务行被按 `period_end` 生效, 等于断言"财报在报告期末已公开", 使历史换手率使用当日尚未公开的股本 → 回测换手类因子系统性偏移。
修复: 可用日只认 `announce_date`; 公告日未知(含整表无该列)的行退出历史表, 由 §3.4 允许的最新维表股本兜底。

### OPS-1 / P1 — 前端测试套件从未进入 CI
123 个前端测试文件、**实测 843 个用例全部通过**, 但 CI 前端 job 只跑 `pnpm build`。修复: 在 `ci.yml` 增加阻断式 `pnpm test`。

### FE-01 / P1 — 指数页切换指数后展示上一只指数的日K
标题正确、数据错误 —— 用户会把一只指数的 OHLC 读成另一只的。修复: `placeholderData` 按 symbol 守卫 (复用 `lib/kline.ts` 既有范式) + `chartRows` 按响应标的过滤。

## 4. 未处置的重大风险 (OPEN)

| 编号 | 问题 | 严重度 | 优先级 |
| --- | --- | --- | --- |
| OPS-2 | e2e_prod 测试断言了不存在的 `/health/live`、`/health/ready`、Prometheus `/metrics`、`X-Request-ID`、`audit.jsonl` → **CI 后端门禁当前为红** | High | P0 |
| HA-1 | 市场数据 (Parquet 树) **无任何备份与恢复路径**; 配置备份只恢复"用什么算", 不恢复"算的是什么" | High | P1 |
| HA-2 | 看门狗在桌面版是 `os._exit`, 绕过优雅停机且无 supervisor, 用户必须手动重开应用 | High | P1 |
| OPS-3 | 容器镜像在 main 推送时直接发布到 GHCR, 不依赖 CI, 且文档升级路径正是拉 `:latest` | High | P1 |
| OPS-4 | 无任何漏洞扫描 / SAST / 容器扫描 / SBOM / 依赖更新自动化 | High | P1 |
| ARC-1 | 行情轮询线程在缓存未命中时做全量 Parquet 扫描与指标重算 (阻塞数秒) | High | P1 |
| PERF-1 | 行情轮询线程每轮全量扫描模拟盘订单历史, 且在全局锁内读盘 | High | P1 |
| SEC-003 | 未设访问密码时, 任意内网主机可抢先认领面板 | Medium | P1 |
| QUAL-3 | 异常路径 fail-open, 返回看似合理实则错误的金融数值 (异动排名/ETF 监控轮次/涨跌停基准) | Medium | P1 |
| DATA-003 | 实时 enriched 涨跌幅走原始价口径, 历史路径走前复权口径 | Medium | P2 |

完整 65 项见 `findings.json`。

## 5. 前后变化

| 指标 | 审计前 | 审计后 |
| --- | --- | --- |
| 路径穿越可删除任意 .json | 存在 (已复现) | **已修复** + 4 条回归用例 |
| 自定义源涨跌幅单位判定 | 数值启发式 (可 100 倍错) | **取消推断**, 按价格重算或置空 |
| 历史股本可用日 | 公告日缺失时按报表期 | **只认公告日**, 缺失即降级 |
| 前端测试进入 CI | 否 | **是** (阻断式) |
| 指数切换数据正确性 | 标题新、数据旧 | **按 symbol 守卫** |
| 回归测试 | — | 新增 2 个文件 / 16 个用例 (7 + 4 + 5) |

## 6. 发布建议

**CONDITIONAL_GO**。理由:
- 无未处置 P0 (本次修复的 SEC-001 与 DATA-001 已 VERIFIED);
- 核心链路 (行情 → enriched → 监控 → 回测) 的既有回归测试未因本次修改失败;
- **但** 仓库自带 CI 后端门禁在审计前即为红 (OPS-2, 8 项失败), 需先由维护者选定"实现这四个可观测性端点"或"删除这四条断言"; 未做第三方渗透测试 (EXTERNAL_REQUIRED); 长期可用性、真实容灾与生产容量均未验证 (NOT_EXECUTED)。
- 99.99% 只能作为**设计目标**: 365 天理论不可用预算约 52.56 分钟, 但计算必须基于已确认的 SLI、统计窗口与排除规则; 代码修改、短时压测或一次演练都不能证明全年达标。

## 7. 交付物索引

| 文件 | 内容 |
| --- | --- |
| `00-executive-summary.md` | 本文件 |
| `01-scope-baseline-and-coverage.md` | 范围、环境、版本、工具、已执行/未执行清单、覆盖矩阵 |
| `02-architecture-review.md` | 当前架构、核心链路、数据流、信任边界、目标架构、ADR |
| `findings.json` | 65 项问题的统一字段契约清单 |
| `03-remediation-plan.md` | 优先级、依赖、批次、排期、资源、迁移与回滚 |
| `04-remediation-ledger.md` | 逐条可追踪台账 (编号→变更→测试→证据→残余风险) |
| `05-verification-report.md` | 每条实际命令、环境、结果、日志位置、前后对比、未执行项 |
| `06-residual-risk-and-release-decision.md` | 残余风险、补偿控制、上线条件、GO/NO_GO |
| `security/` | 威胁模型、扫描结果摘要、第三方测试交接材料 |
| `operations/` | SLI/SLO、告警目录、值班与 Runbook、备份恢复、应急响应 |
| `reviews/` | 文档版本记录、评审意见模板、签字确认模板 |
