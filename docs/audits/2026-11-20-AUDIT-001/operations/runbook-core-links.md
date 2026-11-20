# 运维 — 值班手册与核心链路 Runbook

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20
本文档中的命令**均按本仓库实际技术栈生成** (Python 3.11+ / uv / FastAPI / Polars / DuckDB / React / Vite), 并已核对 API 路径确实存在。

## 0. 通用前置

```powershell
# 后端依赖 (首次或变更后)
cd backend
uv sync --extra dev --frozen

# 前端
cd frontend
pnpm install --frozen-lockfile
pnpm build
```
适用版本: v0.3.37 / `8cd1010` | 运行环境: Windows 10.0.26300 x64 (桌面版与本地), Linux (Docker)
数据目录: `DATA_DIR` 环境变量, 默认 `./data`; Docker 中为 `/app/data`
证据保存: `docs/audits/<审计编号>/evidence/`

## 1. 日常巡检 (每交易时段 3 次)

| 步骤 | 命令 | 预期输出 | 失败判断 |
| --- | --- | --- | --- |
| 1.1 进程存活 | `curl -s http://127.0.0.1:3018/health` | JSON 含 `"status":"ok"` | 无响应 → §2.4; `ok` 但面板无数据 → §2.1 |
| 1.2 行情新鲜度 | 打开面板行情条, 或 `curl -s http://127.0.0.1:3018/api/intraday/quote-status -b <session-cookie>` | 交易时段内数据年龄 < 60s | 过大 → §2.1 |
| 1.3 管道状态 | `curl -s http://127.0.0.1:3018/api/pipeline/status -b <session-cookie>` | 今日作业状态为已完成 | 未完成 → §2.2 |
| 1.4 负载计数 | `curl -s http://127.0.0.1:3018/api/data/workload -b <session-cookie>` | 各组计数正常增长 | 出现 `work_busy` 大量出现 → §2.1 |
| 1.5 磁盘 | `Get-PSDrive C` (或 `df -h` ) | 数据目录所在卷可用空间 > 15% | 不足 → §2.5 |

## 2. 故障处置

### 2.1 行情停更 / 拉取失败 (AL-01/02/11)

- 适用版本: v0.3.37 及以上 | 前置: 面板可访问 | 责任角色: 值班人 → 后端
- **诊断**
  ```powershell
  Get-Content data/logs/backend.log -Tail 200 | Select-String "行情|quotes|拉取失败|重试"
  ```
  重点看: `行情拉取失败: {原因}`、`自定义实时行情拉取失败`、`能力不可用`。
- **处置顺序**
  1. 确认数据源配置: 面板 → 设置 → 数据源, 检查当前生效源与所需数据集是否可用 (能力矩阵)。
  2. 若供应商侧异常 → 等待恢复; 不切源则历史 enriched 不受影响, 但盘中无新数据。
  3. 若自定义源失败 → 用面板的"测试连接"确认; 检查 `pct_unit` 是否声明 (见 `../04-remediation-ledger.md` §2)。
  4. 若因写锁/采集闸卡死 → §2.4。
- **停止条件**: 连续 2 个交易时段无法恢复 → 升级为事件, 按 `backup-restore-and-incident.md` §2.4 定级。
- **回滚/升级**: 无配置变更时无需回滚; 若临时切换数据源, 恢复后需回到原配置并重跑当日管道。

### 2.2 管道未执行 / 超时 (AL-03/04)

- **手动补跑** (端点已核对存在: `main.py:264` 注释与 `frontend/src/pages/Screener.tsx:1305` 均引用)
  ```powershell
  curl -s -X POST http://127.0.0.1:3018/api/pipeline/run -H "Content-Type: application/json" -b <session-cookie> -d "{}"
  ```
  预期: 返回作业标识; 随后 `GET /api/pipeline/status` 可见作业推进。
- **失败判断**: 返回 503 且 `code=work_busy` → 有重任务在跑, 等待后重试; 返回 4xx → 检查参数与权限。
- **强制重算某个历史区间**: 面板 → 数据 → 增强数据 → 重建 (对应 `frontend/src/components/data/EnrichedRebuildPanel.tsx`)。
- **停止条件**: 单日重建超过 2 小时未完成 → 停止并转 §2.4 (可能是楔死)。
- **注意**: 重算期间不要并发跑每日管道 (HA-003 风险); 重建会精确失效相关缓存 (`repo.invalidate_enriched_asset`、`ScreenerService.clear_history_cache`、`strategy_cache.clear_cache`), 刷新前端即可。

### 2.3 数据缺口 (AL-05)

- **扫描** (端点已核对: `backend/app/api/data.py:118`)
  ```powershell
  curl -s -X POST http://127.0.0.1:3018/api/data/integrity/scan -H "Content-Type: application/json" -b <session-cookie> -d '{"lookback_days":30,"asset_types":["stock"]}'
  ```
- **修复** (端点已核对: `backend/app/api/data.py:128`)
  ```powershell
  curl -s -X POST http://127.0.0.1:3018/api/data/integrity/repair -H "Content-Type: application/json" -b <session-cookie> -d '{"report_id":"<扫描返回的 id>"}'
  ```
- **预期**: 修复走"暂存校验后落盘"(`repair_daily.repair_integrity_issues`), 并精确失效受影响缓存。
- **失败判断**: 修复作业失败 → 不要手工改 Parquet; 改用面板的"日 K 修补"(`frontend/src/components/data/RepairDailyPanel.tsx`) 针对单日。
- **停止条件**: 连续两次修复后仍有缺口 → 可能是供应商历史区间不可得, 记录为数据限制并通知用户。

### 2.4 进程楔死 / 看门狗退出 (AL-06)

- **现象**: 后端进程退出, 退出码 70 (`watchdog.py:104-109`); 桌面版**整个应用退出**。
- **诊断**: `Get-Content data/logs/backend.log -Tail 300 | Select-String "watchdog|wedged|卡死"`
- **处置**
  - Docker: 容器 `restart: unless-stopped` 会自动拉起; 检查 `docker compose logs --tail 200 app`。
  - 桌面版: **手动重开应用** (这是当前的预期恢复方式, HA-002 未修复)。
  - 确认残留的 enriched 发布标记已被自愈: 日志中应有 `已恢复上次中断的 enriched 发布标记`。
- **停止条件**: 24 小时内出现 2 次 → 升级为事件, 按 `backup-restore-and-incident.md` §2.4。
- **升级处理**: 若伴随机型闸 (polars collect gate) 卡死, 检查 Polars 版本与 CPU 指令集 (README 提到旧 CPU 需 `uv sync --extra legacy-cpu` 装 `polars[rtcompat]`)。
- **回滚**: 无配置变更无需回滚。

### 2.5 资源水位 (AL-07/08)

- **内存**: `Get-Process python | Select-Object Id,CPU,WS` 或容器 `docker stats`。看门狗默认 30s 探测、连续 2 次失败即退出 (`config.py:223-226`)。
- **磁盘**: 数据目录每个交易日都在增长; 无自动上限控制。**这是 AL-08 存在的根因** —— 需靠人工清理或快照保留策略。
- **处置**: 清理旧的分区前先确认 `data/` 中哪些是原始行情 (不可再生) 哪些是派生的 (可重算)。
- **停止条件**: 磁盘 < 5% → 立即停止一切重算与备份, 先扩容。
- **升级**: 同盘快照挡不住磁盘故障, 备份目标应为异盘 (`backup-restore-and-incident.md` §2)。

### 2.6 锁争用 (AL-10)

- **查询**: `curl -s http://127.0.0.1:3018/api/backtest/mining/lock -b <session-cookie>` (端点已核对)
- **恢复**: `POST /api/backtest/mining/lock/recover` (仅在满足陈旧判定时生效)
- **强制释放**: `POST /api/backtest/mining/lock/force-release` (端点已核对) —— **会中止持有者的挖掘任务, 属破坏性操作, 需确认持有者确已僵死后执行**
- **失败判断**: 强制释放后仍显示被占用 → 存在第二个后端实例; 检查是否有多进程指向同一 `DATA_DIR` (HA-003)。

## 3. 核心链路 → 排查入口对照

| 链路 | 症状 | 先看哪里 | 再看哪里 |
| --- | --- | --- | --- |
| T1 实时行情 → 盘中决策 | 行情不更新 / 告警延迟 | `backend.log` 行情相关行 | `/api/data/workload` 的 http.kline 分组; `quote-status` |
| T2 日线管道 | 盘后数据没更新 | `/api/pipeline/status` | `backend.log` 管道段; `/api/data/integrity/scan` |
| T3 策略选股 | 结果为空或数值异常 | 面板"策略结果"的 as_of 与数据日期是否一致 | `/api/screener/cached` 与 enriched 分区日期 |
| T4 回测 | 结果与实盘对不上 | 回测的 `as_of` 与 enriched 生成代际 | 换手率口径 (本次 DATA-002 变更后需重算历史) |
| T5 监控与通知 | 规则不触发 / 文案异常 | 面板"监控规则"页的规则命中记录 | `backend.log` 通知投递段; 通知适配器配置 |
| T6 模拟盘 | 下单无响应 / 持仓不对 | `/api/backtest/mining/lock` 是否阻塞 | `backend.log` 模拟盘段; 订单目录文件数是否异常增长 (PERF-001) |

## 4. 升级与联系

| 场景 | 升级对象 | 附带材料 |
| --- | --- | --- |
| 凭据/会话被删 (AL-09) | 安全负责人 + 后端负责人 | `backend.log` 对应时段; `data/user_data` 的文件清单与 mtime |
| 数据疑似损坏 | 后端负责人 + QA | `integrity/scan` 报告 id; 受影响分区列表 |
| 连续 2 个时段无法恢复 | SRE | 本手册 §2 各步骤的执行记录 |
| 疑似安全问题被外部触发 | 安全负责人 | 见 `../security/third-party-test-handover.md` 的记录模板 |
