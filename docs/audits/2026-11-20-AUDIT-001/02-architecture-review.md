# 02 — 架构评审

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20

## 1. 系统上下文

```text
┌────────────── 浏览器 (React 18 + TS + Vite + TanStack Query) ──────────────┐
│  pages/  components/  custom/(页面包)  extensions/(运行时页面包)             │
│  lib/api.ts (唯一请求封装)  lib/queryKeys.ts  lib/useQuoteStream.ts(SSE)   │
└───────────┬──────────────────────────────────────────┬─────────────────────┘
            │ REST + SSE                               │ 静态 dist (SPA)
            ▼                                          ▼
┌───────────────────────── FastAPI (backend/app/main.py) ───────────────────┐
│ 中间件外→内: SecurityHeaders → CORS → BodyLimit → auth → workload        │
│ api/ (42 薄层路由)   app.state.* 单例   SSE /api/events + /api/intraday/…  │
├───────────────────────────────────────────────────────────────────────────┤
│ services/ (108)  quote_service  kline_sync  screener  strategy  monitoring │
│                  integrity  repair  pipeline_jobs  notify  ext_*  safe_http│
├───────────────────────────────────────────────────────────────────────────┤
│ strategy/ (52)  engine  monitor  registry  custom_signals  paper  sandbox │
│ backtest/  (24)  engine  matrix  optimizer  worker  fundamentals          │
│ indicators/(3)   pipeline (enriched 全量计算)                             │
│ tickflow/  (9)   DataStore  KlineRepository  capabilities  rate_limits    │
│ data_providers/   registry + custom/ + plugins/(fuyao, tdxquant)           │
│ plugins/ extensions/ jobs/ factors/ custom/                              │
├───────────────────────────────────────────────────────────────────────────┤
│ 存储: data/kline_daily  kline_daily_enriched  kline_minute  adj_factor   │
│       financials  depth5  instruments  user_data/  strategies/           │
│       DuckDB = :memory: 视图层 (无持久化)                                │
└───────────────────────────────────────────────────────────────────────────┘
            │ HTTPS (供应商)                     │ 本地 Parquet
            ▼                                   ▼
   TickFlow SDK / 自定义 HTTP 源 / fuyao / tdxquant      磁盘 (唯一系统记录)
```

**关键结构事实** (均有代码依据):
1. **DuckDB 不提供持久化**: `backend/app/tickflow/repository.py:162` 使用 `duckdb.connect(database=":memory:")`, DuckDB 仅作 Parquet 之上的查询/视图层。
2. **Parquet 树是唯一系统记录**: `config_backup._collect_backup_files` (services/config_backup.py:239-286) 不含任何行情/指标/财务数据 → HA-001。
3. **部署形态三种, 自愈假设只覆盖其一**: Docker (`restart: unless-stopped`)、桌面版 (后端是 GUI 进程内的一个线程, desktop.py:441-444)、开发脚本 (dev.ps1/dev.sh) → HA-002。
4. **数据源已插件化**: `data_providers/registry.py` (内置 TickFlow) 与 `data_providers/custom/loader.py` (插件, 返回 ManagedProvider) 是两套并行入口 → ARC-004。

## 2. 核心交易链路 (候选清单, **待业务确认**)

按 CONTRIBUTING §2.2 的主数据流, 识别出 6 条候选核心链路:

| 编号 | 链路 | 入口 | 主要阶段 | 本次审计的关键风险点 |
| --- | --- | --- | --- | --- |
| **T1** | 实时行情 → 盘中决策 | `quote_service._poll_loop` → `_fetch_full_market_quotes` → `_process_full_market_records` → 写 kline_daily/enriched → SSE → 监控评估 | 拉取 → 标准化 → 落盘 → 增量指标 → SSE → 监控/告警 | ARC-001 (轮询线程全量扫 Parquet)、PERF-001 (每轮扫订单历史且持锁)、DATA-001/002/003 (口径) |
| **T2** | 日线管道 (盘后) | `jobs/daily_pipeline` (APScheduler, Asia/Shanghai) → `kline_sync` → `indicators.pipeline` → enriched 分区发布 | 同步 → 校验 → 复权 → 全量指标 → 原子发布 | HA-003 (双实例并发写)、DATA-002 (历史股本泄露) |
| **T3** | 策略选股 | `api/screener` → `ScreenerService` → DuckDB 过滤 → 策略注册表 | 候选池 → 条件 → 评分 → 排序 → 结果缓存 | QUAL-003 (fail-open 返回错值)、ARC-001 (历史窗口) |
| **T4** | 回测与步进优化 | `api/backtest` → `backtest/engine` (+ `matrix`) → 成交模拟 → 证据面板 | 信号生成 → 成交模拟 → 费用/T+1/涨跌停约束 → 证据 | DATA-004 (换手率兜底)、QUAL-005 (页面重算排序方向) |
| **T5** | 监控与通知 | `strategy/monitor` + `api/monitor_rules` → `notify_adapter` / `webhook_adapter` / `wecom_bot_service` | 规则 → 实时快照 → 去重/冷却 → 投递 | PERF-001、QUAL-003 |
| **T6** | 模拟盘 | `api/paper` → `strategy/paper` + `paper_auto` → 订单/持仓/结算 | 下单 → T+1 → 撮合 → 结算 → 通知 | PERF-001 (每轮全量扫订单文件) |

> 状态: 上述清单由代码与文档推导, **未经业务确认**。若业务方认定其他链路为核心, 需重新评估对应维度的风险权重。

## 3. 信任边界

| 边界 | 位置 | 现有控制 | 审计评价 |
| --- | --- | --- | --- |
| B1 浏览器 ↔ 后端 | `main.py:974-1056` auth 中间件 | 单密码会话 (PBKDF2 200k 轮) + 同源/Referer/Sec-Fetch-Site 校验 + BodyLimit + CSP + 安全响应头 | 控制良好; 但 CSRF 双提交分支不可达 (SEC-005), Cookie 缺 Secure (SEC-004) |
| B2 API Token ↔ 后端 | `services/api_gateway.py` | 六档 scope + deny-by-default + 令牌只存 SHA-256 摘要 + 限流 + 网络门禁先行 + 路径形态过滤 | 控制良好 (SEC agent 复核项 5-10 全部通过) |
| B3 后端 ↔ 外部供应商 | `services/safe_http.py` | scheme 白名单 + 私网/链路本地阻断 + DNS 解析 + 重定向剥离凭据 | 控制良好; 但 `global_market_data.py` 绕开该封装自建客户端 (PERF-003) |
| B4 后端 ↔ 磁盘 | `api/analysis.py` 等路径参数 | **不统一**: 多数端点有白名单+包含检查, `analysis.py` 删除接口没有 | **缺口 (SEC-001, 已修复)** |
| B5 后端 ↔ 凭据文件 | `secrets_store.py` / `api/auth.py` | 0600 + 原子写 + 响应掩码 + 备份默认不含 secrets | 控制良好; 但会话 token 明文落盘 (SEC-006) |
| B6 插件 ↔ 核心 | `plugins/`, `data_providers/custom/loader.py` | 加载失败隔离 (loader.py:53-78) + 缺能力 fail-closed | 控制良好; 但四服务硬编码 "fuyao" (ARC-005), 两套 provider 入口 (ARC-004) |
| B7 页面包 ↔ 前端宿主 | `extensions/runtime.tsx` + `api/ext_pages.py` | 仅 React 错误边界 | **无能力边界 (FE-004 / SEC-007)** —— 页面包可调用全部 /api/ |
| B8 归档 ↔ 导入 | `services/config_backup.py:431-524` | 无 `extractall`; 路径白名单 + 逐文件/逐包/总量上限 + 截断读取 + 导入后重新做 AST 校验 | 控制良好 (SEC agent 复核项 15-16 通过) |

## 4. 架构问题 (证据摘要)

### 4.1 高频问题: 同一份契约在多条路径上不一致
审计中最有价值的一类发现不是"缺功能", 而是**同一份规则被复制到多处, 其中一处漏了**:

| 契约 | 正确实现 | 漏/错实现 | 编号 |
| --- | --- | --- | --- |
| 菜单标识白名单 | `analysis.py:182-183` (写) | `analysis.py:193-199` (删) | SEC-001 |
| 比例字段单位 | `amplitude`/`turnover_rate` 未声明即置 None | `change_pct` 未声明时按数值猜 | DATA-001 |
| 股本可用日 | 公告日不晚于交易日 | 公告日缺失时按报表期 | DATA-002 |
| provider 获取 | `custom_sources.get_provider` (插件) | `registry.get_provider` (仅内置) | ARC-004 |
| 能力声明 | `capabilities.py` + preferences 路由 | 四服务硬编码插件名 | ARC-005 |
| 查询键 | `lib/queryKeys.ts` 集中工厂 | 12 处内联字面量 | QUAL-002 / FE-008 |
| 请求封装 | `lib/api.ts` (超时/错误/401) | 5 处裸 fetch | QUAL-001 / FE-009 |

**结论**: 建议把"契约的唯一权威位置"作为后续治理的第一原则 —— 与 CONTRIBUTING §1.1 (不建平行实现) 一致。

### 4.2 实时路径与按需路径未分离
`quote_service` 的轮询线程复用 `ScreenerService._load_enriched_history` (main.py:467), 而后者在缓存未命中时是 `scan_enriched_parquet('**/*.parquet') + compute_indicators` 的慢路径 (screener.py:333, 注释标 ~5s)。实时热路径因此可能被阻塞数秒 (ARC-001)。同理, 模拟盘钩子在轮询线程内做文件扫描 (PERF-001)。
**这是本次审计中唯一一处"架构性"而非"点状"的性能根因**, 因此优先级高于其单点严重度。

### 4.3 API 层持有存储布局
`api/ext_data.py` (59 个模块级函数) 直接读 Parquet、直接开 DuckDB、直接遍历 `kline_daily/date=*` 分区; `api/watchlist.py:727` 反向从 API 模块 import 服务所需函数 (ARC-002)。这让"存储布局"这一本应由 `tickflow/repository.py` 独占的知识, 扩散到了路由层。

### 4.4 自愈与降级语义不完整
- 看门狗 `os._exit` 假设存在 supervisor, 桌面版没有 (HA-002);
- `/health` 的 `status` 是常量, 不反映调度器/指标预热/锁状态 (HA-004);
- 采集锁未命中时只降级"挖掘", 调度器与行情写盘照旧启动 (HA-003)。

## 5. 目标架构 (建议, 非本次实施范围)

```text
目标: 把"契约唯一权威"与"实时/按需路径分离"两条原则落到结构上

1) 契约层 (新增薄层, 只放判定, 不放业务)
   app/contracts/units.py      —— 比例字段单位判定 (声明 > 重算 > 不可算)
   app/contracts/pathsafe.py   —— 路径参数白名单 + 目录包含检查 (供所有文件类端点复用)
   app/contracts/availability.py —— 公告日/可用日判定

2) 存储唯一权威
   tickflow/repository.py 持有全部磁盘布局知识
   api/ext_data.py 的读/聚合逻辑下沉到 services/ext_data.py
   服务层不得从 api/ 反向 import (可在 CI 加静态检查)

3) 实时路径与按需路径分离
   services/realtime_history.py —— 后台预热器, 填充 screener 既有 _history_cache
   轮询线程只读已物化的数据帧; 慢路径永不进入轮询线程

4) 可用性契约
   GET /health   —— 纯 liveness (不变)
   GET /ready    —— indicators_ready / scheduler_running / quote_running / degraded
   GET /metrics  —— Prometheus 文本 (工作负载计数、内存、队列深度、管道新鲜度)
   X-Request-Id  —— 中间件铸造, contextvars 注入日志
   data/user_data/audit.jsonl —— 安全审计 (SEC-002 / HA-005 合并建设)

5) 门禁
   backend: pytest (现有) + ruff (先非阻断, 清存量后转阻断)
   frontend: vitest (本次已接入, 阻断) + eslint (先非阻断)
   supply-chain: pip-audit + pnpm audit + Trivy + CycloneDX SBOM + Dependabot
```

## 6. ADR

### ADR-A1 — 文件类端点的路径参数统一走"白名单 + 目录包含"双重校验
- 状态: **已采纳并部分实施** (analysis.py 已落地; 其余端点待系统化)
- 背景: 写入侧有校验而删除侧没有 (SEC-001), 说明校验是逐端点手工添加的, 必然遗漏。
- 决策: 所有以标识符拼路径的端点必须 (1) 通过与该资源模型一致的标识符白名单; (2) 解析后目标必须直接位于该资源目录下 (`p.parent == base.resolve()`), 范式照 `api/pages_admin.py:47-58`。
- 后果: 新增端点有两个可复用的现成范式; 代价是每个端点多两行校验。
- 影响: SEC-001 修复采用此范式。

### ADR-A2 — 比例字段单位: 声明 > 重算 > 不可算, 禁止任何按数值的推断
- 状态: **已采纳并实施** (DATA-001)
- 背景: §3.1 明令禁止启发式换算, 但实现与文档各自矛盾 (代码用截面中位数, 文档既说"不猜"又说"按中位数归一")。数值无法区分 `0.05` 是 0.05% 还是 5%。
- 决策: 单位只认显式声明 (`pct_unit`); 未声明时, `change_pct` 由价格比值重算 (与单位无关), 其余比例列置空交下游重算。任何按数值分布的推断一律删除。
- 后果: 依赖旧启发式的自定义源需显式声明 `pct_unit`; 换来的是"永不出现静默 100 倍错误"。
- 影响: provider.py、test_custom_pct_units.py、docs/custom-data-source.md。

### ADR-A3 — 历史股本以公告日为唯一可用日依据, 未知即不可用
- 状态: **已采纳并实施** (DATA-002)
- 背景: §3.4 要求历史换手率优先使用"公告日不晚于目标交易日"的股本, 且报表期不得泄露; 原实现用 `period_end` 兜底缺失的公告日。
- 决策: 可用日 = `announce_date`; 公告日未知 (含整表无该列) 的行不参与历史匹配, 由 §3.4 允许的最新维表股本兜底。
- 后果: 已落盘的旧 enriched 分区不回溯修正, 需在发版说明中提示重算历史换手率。
- 影响: share_capital.py、test_financial_shares.py。

### ADR-A4 — 门禁按"先非阻断、存量清零后转阻断"推进
- 状态: **建议 (部分实施)**
- 背景: 本次实测 ruff 约 700 条告警、eslint 8 errors + 227 warnings; 直接接入阻断式门禁会让 CI 立刻变红 (而 CI 后端门禁当前已经是红的, 见 OPS-002)。
- 决策: (a) 可立即转阻断的门禁 (前端 vitest, 实测全绿) 直接接入; (b) 存量大的门禁 (ruff / eslint) 先以非阻断方式接入并按规则分批 ratchet, 清零后转阻断; (c) 任何门禁接入前必须先给出存量数字与通过状态。
- 后果: 门禁状态可预期, 不会用"接入即红"换取虚假的形式合规。
- 影响: .github/workflows/ci.yml (已加 vitest); ruff/eslint 待清存量。

### ADR-A5 — 数据备份与配置备份分离, 前者是发布阻断项
- 状态: **建议 (未实施)**
- 背景: 现有备份只覆盖"用什么算" (配置/策略/凭据), 不覆盖"算的是什么" (行情/指标/财务); 磁盘故障即多年数据归零。
- 决策: 数据快照作业与配置备份并列, 作为独立能力与独立发布门禁项; 快照目标需支持异盘 (同盘快照挡不住磁盘故障)。
- 后果: 需要额外磁盘预算与保留策略; 换来可恢复性。
- 影响: HA-001、OPS-010。
