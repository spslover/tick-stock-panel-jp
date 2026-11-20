# 03 — 修复计划

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20 | 执行模式 `audit_and_fix`

## 1. P0 / P1 处置与发布阻断建议

| 编号 | 问题 | 状态 | 阻断发布? | 处置 |
| --- | --- | --- | --- | --- |
| SEC-001 | 路径穿越 → 任意 .json 删除 | **IMPLEMENTED / VERIFIED** | 曾是, 已解除 | 本次已修复 (见 `04-remediation-ledger.md` §1) |
| DATA-001 | 单位启发式 → 静默 100 倍错误 | **IMPLEMENTED / VERIFIED** | 曾是, 已解除 | 本次已修复 |
| OPS-2 | CI 后端门禁为红 (8 项预先存在失败) | OPEN | **是** | 需维护者在"实现四个可观测性端点"与"删除四条断言"之间二选一 (见 §2.1) |
| DATA-002 | 历史股本报表期泄露 | **IMPLEMENTED / VERIFIED** | 曾是, 已解除 | 本次已修复 |
| OPS-1 | 前端测试不进 CI | **IMPLEMENTED** | 曾是, 已解除 | 本次已接入 (实测 843 全绿) |
| FE-01 | 指数切换展示错标的日K | **IMPLEMENTED / VERIFIED** | 曾是, 已解除 | 本次已修复 |
| HA-1 | 数据无备份 | OPEN | 是 (建议) | 见 §2.2 批次 C |
| HA-2 | 桌面版看门狗硬杀 | OPEN | 是 (建议) | 见 §2.2 批次 C |
| OPS-3 | 镜像发布不依赖 CI | OPEN | 是 (建议) | 见 §2.2 批次 B |
| OPS-4 | 无供应链扫描 | OPEN | 是 (建议) | 见 §2.2 批次 B |
| ARC-1 / PERF-1 | 实时线程做重活 | OPEN | 否 (但影响盘中可用性) | 见 §2.2 批次 D |
| SEC-003 | 内网抢先认领 | OPEN | 否 (但属 P1) | 见 §2.2 批次 B |
| QUAL-003 | fail-open 返回错误金融值 | OPEN | 否 (但属 P1) | 见 §2.2 批次 D |

**发布阻断建议**: 在 OPS-2 处置完成 (CI 后端门禁回绿) 之前, 本仓库的自动化门禁不构成有效的回归防线; 该项应作为**发布前置条件**而非普通缺陷。

## 2. 实施批次、依赖与验收

批次按"改动面小→大"与"解除后续阻塞"排序; 每个批次独立可回滚, 完成后立即跑相关测试。

### 2.1 批次 A — 已完成 (本次审计实施)

| 批次 | 内容 | 依赖 | 验收条件 | 回滚条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| A1 | SEC-001 路径穿越修复 | 无 | `test_analysis_menu_path_traversal.py` 7 通过; `test_analysis_menu_url_template.py` 5 通过 | 任一既有 analysis 菜单用例失败 | **VERIFIED** |
| A2 | DATA-001 单位判定修复 | 无 | `test_custom_pct_units.py` 21 通过 | 自定义源集成用例失败 | **VERIFIED** |
| A3 | DATA-002 股本可用日修复 | 无 | `test_financial_shares.py` 19 通过 | 换手率相关用例失败 | **VERIFIED** |
| A4 | OPS-1 前端测试进 CI | A2/A3 无依赖 | `vitest run` 123 files / 843 tests 通过 (已实测); CI 侧需人工确认步骤出现 | CI 因 vitest 失败 | **IMPLEMENTED** (CI 侧待人工确认) |
| A5 | FE-01 指数切换修复 | 无 | 新增 vitest 通过 + 既有 `Indices.test.tsx` 通过; 回退修复后新用例失败 (已实测) | 既有 Indices 用例失败 | **VERIFIED** |

批次 A 合计: 5 个问题, 新增 2 个测试文件 / 16 个用例。

### 2.2 批次 B — 门禁与供应链 (建议 T0 起 1 个迭代)

| 项 | 编号 | 内容 | 验收 | 回滚 |
| --- | --- | --- | --- | --- |
| B1 | **OPS-2** | 维护者二选一: (A) 实现 `/health/live`、`/health/ready`、Prometheus `/metrics`、`X-Request-ID`、`audit.jsonl` 并保留 e2e_prod 断言; (B) 删除这四条断言并在 issue 登记缺口 | `pytest tests/e2e_prod/test_tier1_health_auth_gateway.py -q` 全绿 | 保留断言但改用 skip 会掩盖缺口, 不推荐 |
| B2 | OPS-10 | CI 增加 `python3 scripts/sync_versions.py --check`; 新增 `docs/release-gate.md` | 只改 package.json 版本即 CI 失败 | 移除步骤 |
| B3 | OPS-11 | ci.yml 增加 `permissions: contents: read` | workflow run 显示最小权限 | 移除 |
| B4 | OPS-8 | ruff / eslint 以**非阻断**方式接入 CI, 并输出存量基线数字 | CI 日志出现步骤且不阻断 | 移除步骤 |
| B5 | OPS-4 | Dependabot (github-actions / pip / npm) + `pip-audit` + `pnpm audit --audit-level=high` + Docker Trivy + CycloneDX SBOM | 故意引入易受攻击版本时扫描作业失败 | 作业改 continue-on-error |
| B6 | OPS-6 | Action 固定到 commit SHA; 基础镜像固定 digest; pnpm 固定 9.10.0 | workflow 仍可运行 | 回滚 pin |
| B7 | OPS-7 | 去掉 Dockerfile 两处 `||` 回退与 `pnpm-lock.yaml*` 通配符 | 锁文件不同步时构建失败 | 恢复回退 (会重新引入不可复现构建) |
| B8 | OPS-3 | 用 `workflow_run` 门禁 GHCR 发布; 部署文档补回滚到 `:sha-` 标签 | 失败提交不产生新标签 | 恢复原触发 |
| B9 | OPS-14 | Dockerfile HEALTHCHECK + compose healthcheck | `docker compose ps` 显示健康状态 | 移除 |
| B10 | SEC-003 | 一次性认领码或强制 AUTH_PASSWORD; 未配置状态下 API 降级只读 | 192.168.x 来源在未设密码时拿不到变更能力 | 保留原行为 (需显式接受风险) |

### 2.3 批次 C — 数据安全与可用性 (建议 T0 起 1-2 个迭代)

| 项 | 编号 | 内容 | 验收 | 回滚 |
| --- | --- | --- | --- | --- |
| C1 | **HA-1** | 数据快照作业 (每日, 管道后) + 保留策略 + 恢复入口 (复用 Import/Backup UI) | 在 data/ 副本上做"快照→删除→恢复", enriched 行数一致且单分区 sha256 相同; `/api/screener/cached` 总量一致 | 作业可由偏好关闭; 恢复为显式用户动作, 覆盖前需确认 |
| C2 | **HA-2** | 桌面模式下由 desktop.py 监督 uvicorn 线程重启, 替代 `os._exit` | 故障注入: 连续 3 次探测失败后桌面进程存活并重启后端 | 回滚看门狗接线 |
| C3 | HA-3 | 数据目录级排他锁门控 start_scheduler; 持锁时仅启动 API | 双实例对同一 DATA_DIR 时第二个不注册 daily_pipeline 作业 | 锁是增量的, 移除即恢复 |
| C4 | HA-4 + OPS-9 + SEC-002 + HA-5 | 可观测性四件套: `/ready`、Prometheus `/metrics`、`X-Request-ID`、`audit.jsonl` + 心跳 | `/ready` 在调度器禁用时非 200; `/metrics` 可解析且计数随请求变化; 日志含请求 ID | 增量新增, 可单独移除 |
| C5 | OPS-13 | MCP server 加 CI 作业 (至少工具 schema 快照) | CI 作业为绿 | 移除作业 |
| C6 | OPS-12 | 修正文档中的脚本路径与 README 徽章文案 | 修正后的命令可执行 | 文档回滚 |

### 2.4 批次 D — 数据正确性与实时路径 (建议 T0 起 2-3 个迭代)

| 项 | 编号 | 内容 | 验收 | 回滚 |
| --- | --- | --- | --- | --- |
| D1 | **QUAL-003** | abnormal_moves 区分"无指数数据"与"指数平盘"; paper/lots 无法解析资产类型时 4xx 拒绝 | 异常路径断言返回显式降级/未知态, 而非 0.0 / 'stock' | 回滚 |
| D2 | **ARC-001** | 行情历史加载移出轮询线程 (后台预热器填充既有 TTL 缓存) | 历史加载器 stub 抛异常时 evaluate() 仍能评估; evaluate() 无 Parquet 读 | 回滚 |
| D3 | **PERF-1** | 模拟盘待处理市价单内存索引; 订单钩子移出轮询线程 | 热路径不再调用 load_orders; 5000 历史订单下前后耗时对比 | 回滚 (旧行为仅是慢) |
| D4 | DATA-003 | 实时 enriched 涨跌幅统一用复权 close/prev_close | 除权日构造用例中 enriched.change_pct 与历史路径相等 | 回滚 |
| D5 | DATA-004 | 矩阵换手率兜底改为按日期 asof 匹配历史股本, 或保持 NaN 并告警 | 矩阵换手率随日期变化或为 NaN | 回滚 (仅影响兜底分支) |
| D6 | DATA-005 | minute_adjust 复用 replace_with_retry + uuid 临时名 | mock 两次 PermissionError 后成功; 并发不产生同名临时文件 | 回滚 |
| D7 | PERF-002 | 市场总览聚合改为 Polars 表达式 | golden fixture 输出等价 + 同数据前后计时对比 | 回滚 |
| D8 | PERF-003 / PERF-004 | 共享 httpx 客户端 + Semaphore + single-flight; create_task 保留强引用 | 并发上限被遵守; 作业在 gc 后仍达终态 | 回滚 |
| D9 | ARC-002 / ARC-003 / ARC-004 / ARC-005 / QUAL-001..008 | 结构性重构 (见各条 recommendation) | 各条自带验证方法 | 逐项回滚 |

### 2.5 前端批次 E (建议 T0 起 2-3 个迭代)

FE-002 (看板日期守卫, 因 Dashboard.tsx 有用户未提交改动而未在本次修改)、FE-003 (单位标注)、FE-004 (页面包能力边界)、FE-005 (回跳校验)、FE-006 (告警键与轮询)、FE-007 (echarts 按需导入)、FE-008 (键集中)、FE-009 (裸 fetch 收敛)。
**注意**: `frontend/src/pages/Dashboard.tsx` 等文件在审计开始时**存在他人未提交改动**, 修改前必须先读取最新内容并保留双方逻辑 (CONTRIBUTING §13)。

## 3. 问题依赖图

```text
OPS-2 (CI 为红) ──┬─→ OPS-8 (接 lint 门禁)  ← 需先清存量
                  └─→ TEST-001 (加强不变量断言)

OPS-4 (无扫描) ──→ OPS-6 (SHA 固定)  ← 需 Dependabot 才能自动升级
        └─→ SEC-008 (依赖上界)

DATA-002 (已修) ──→ DATA-004 (矩阵兜底同源取数)
DATA-001 (已修) ──→ DATA-003 (实时口径统一)
ARC-001 ──→ PERF-002 (两者都会改市场总览/历史加载路径, 建议同批)
HA-4 ──→ OPS-9 ──→ HA-5 / SEC-002 (关联 ID 是共同前置)
FE-004 ──→ SEC-007 (同一信任模型)
SEC-001 (已修) ──→ 端点路径参数的系统化审计 (下一步)
```

## 4. 排期与资源

以 **T0 = 本报告被接受之日** 表示, 日期必须基于明确资源假设。

**资源假设** (需维护者确认):
- 后端 1 人 (熟悉 Polars/DuckDB/Parquet 与本仓数据契约), 前端 0.5 人, CI/安全 0.5 人, QA 0.5 人, SRE 0.25 人。

| 里程碑 | 建议时点 | 内容 | 前置 |
| --- | --- | --- | --- |
| M0 | T0 | 批次 A 完成并合入 (本次已实施) | — |
| M1 | T0 + 3 个工作日 | OPS-2 决策落地; CI 后端门禁回绿 | 维护者决策 |
| M2 | T0 + 1 个迭代 | 批次 B 全量 (门禁与供应链) | M1 |
| M3 | T0 + 1 个迭代 | C1 (数据备份) 与 C4 (可观测性) 启动 | B5 (扫描) |
| M4 | T0 + 2 个迭代 | 批次 D (数据正确性与实时路径) | M1 |
| M5 | T0 + 3 个迭代 | 批次 E 与批次 D9 的结构性重构 | M4 |

**工作量汇总** (角色 × 人日区间, 置信度见各条 estimated_effort):
- 批次 B: CI/安全 4-7 人日 + 后端 3-5 人日
- 批次 C: 后端/SRE 10-18 人日 (C1 3-6、C2 2-3、C4 5-8)
- 批次 D: 后端 12-20 人日 + 前端 2-4 人日
- 批次 E: 前端 4-7 人日

## 5. 数据迁移与兼容性

本次审计实施的 5 项修复中, **两项改变了历史数值语义**, 必须随发布说明交付:

| 变更 | 影响面 | 兼容策略 | 需要用户动作 |
| --- | --- | --- | --- |
| DATA-002 股本可用日 | 已落盘的 enriched 分区的历史 `turnover_rate` | **不做数据迁移** (旧值不回溯), 新旧读取均可; 新值在下次重算生效 | 依赖换手率的历史回测需**重算**历史后复评 |
| DATA-001 单位判定 | 仅影响未声明 `pct_unit` 的自定义 HTTP 源 | 不改存储格式; 旧缓存实时数据不迁移 | 建议在自定义源配置中**显式声明** `pct_unit` |
| SEC-001 路径校验 | 无数据影响 | 无 | 无 |
| OPS-1 CI 门禁 | 无 | 无 | 无 |
| FE-01 前端守卫 | 无 | 无 | 无 |

**expand → migrate → validate → contract**: 本次变更不含 schema 变更, 故不需要 expand/contract 阶段; 但 DATA-002 触发的历史重算属 "migrate", 需在 `/api/pipeline` 的重算入口执行, 且重算期间不得并发跑每日管道 (参照 HA-003 的锁建议)。

## 6. 风险预案与决策点

| 决策点 | 选项 | 建议 | 若不决策的后果 |
| --- | --- | --- | --- |
| D1 (T0+3d) | OPS-2: 实现四个可观测性端点 / 删除四条断言 | **实现** —— 这四条断言表达了作者想要的运维契约, 且与 HA-4/OPS-9/SEC-002 高度重合, 实现后可同时关闭 4 条 OPEN 问题 | CI 长期为红, 门禁失去意义 |
| D2 (T0+1 迭代) | C1 数据快照的存储位置 | **异盘或可移动介质** —— 同盘快照挡不住磁盘故障 | HA-001 只解决一部分 |
| D3 (T0+1 迭代) | ruff/eslint 门禁是否转阻断 | **先非阻断, 分规则 ratchet** | 直接转阻断会让 CI 立刻变红 |
| D4 (T0+2 迭代) | DATA-002 引起的历史重算窗口 | 在非交易时段做, 并与 HA-003 的排他锁一并实现 | 重算与每日管道并发写分区 |
| D5 (持续) | 是否引入 CI 与 OPS-4 的扫描作为发布阻断 | **是**, 但需先建立豁免与误报处理流程 | 供应链风险持续不可见 |

## 7. 延期与无法修复项的补偿控制

| 项 | 状态 | 补偿控制 | 负责人建议 |
| --- | --- | --- | --- |
| 第三方渗透测试 | EXTERNAL_REQUIRED | 见 `security/third-party-test-handover.md` 的范围与准备材料 | 安全负责人 |
| 99.99% 可用性证明 | NOT_EXECUTED | 在 `/ready` + `/metrics` + 心跳落地后, 建立 SLO 告警与容量管理; 理论不可用预算 52.56 分钟/年仅作设计参考 | SRE |
| 跨可用区 / 跨区域容灾 | NOT_EXECUTED | 本项目为单实例自托管形态, 先做本地数据快照 (C1) 与桌面版自愈 (C2) | SRE |
| SCC/依赖上界 | 部分 DEFERRED | 先接扫描 (B5), 再逐步加上下界 (SEC-008) | 后端 |
| 路径参数端点的系统化审计 | DEFERRED | 已修复唯一的确认实例 (SEC-001); 建议下个迭代做一次全量路径参数审计 | 后端 |
| 99.99% 与监管映射 | NEEDS_COMPLIANCE_REVIEW | 未提供法域与规范, 本次不做合规断言 | 合规负责人 |
