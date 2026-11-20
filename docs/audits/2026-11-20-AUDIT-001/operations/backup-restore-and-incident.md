# 运维 — 备份恢复与应急响应

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20

> **现状**: 本项目**没有**数据备份能力 (HA-001, OPEN)。现有"配置备份"(`/api/config-backup/export`) 只覆盖**配置、策略、规则与凭据**, 不覆盖**行情、enriched 指标、财务数据**。
> 本文档因此分两部分: §2 是**当前可执行的补偿控制**, §3 是数据备份能力的**目标设计**。

## 1. 现状: 配置备份能覆盖什么

```text
config_backup._collect_backup_files (services/config_backup.py:239-286) 收集:
  策略 .py  |  user_data/*.json (规则/覆盖/信号/因子/批次/偏好/自选/告警/会话/auth/策略池)
  data_sources/*.yaml  |  analysis_menus  |  声音包  |  ext-data config.json
  ext-page 定义  |  paper/accounts/**
不收集: kline_daily/ kline_daily_enriched/ kline_minute/ kline_index_* kline_etf_*
          adj_factor/ financials/ depth5/ instruments
```
→ 备份恢复的是"**用什么算**", 不是"**算的是什么**"。

- 导出: `GET /api/config-backup/export?categories=<类别>` (端点已核对)
- 导入: 导入时会**强制使全部会话失效** (`api/config_backup.py:154-157`), 导入后需重新登录。
- secrets 类别**默认不导出**, 需显式指定 `categories=secrets`; 该导出含可重放会话 (SEC-006, OPEN)。

## 2. 应急响应 (当前可执行)

### 2.1 分级

| 级别 | 定义 | 响应时限 | 升级对象 |
| --- | --- | --- | --- |
| SEV-1 | 数据损坏 / 凭据泄露 / 面板被接管 | 立即 | 安全 + 后端 + SRE |
| SEV-2 | 核心链路不可用 (管道停摆、行情停更超 1 个时段) | 当日 | 后端 / SRE |
| SEV-3 | 非核心功能异常、界面错误 | 本迭代 | 对应负责人 |
| SEV-4 | 文档、体验类问题 | 计划内 | 对应负责人 |

### 2.2 SEV-1 —— 凭据或会话被删 / 泄露 (AL-09)

**背景**: 本次审计发现并修复了 SEC-001 (`DELETE /api/analysis-menus/{menu_id}` 路径穿越可删除任意 `.json`), 包含 `secrets.json`、`auth.json`、`api_tokens.json`。修复后该路径已封堵, 但**任何**导致这些文件消失或外泄的路径都应按 SEV-1 处理。

- **识别**: `Get-ChildItem data/user_data | Select-Object Name, LastWriteTime, Length`
- **立即动作**
  1. **不要重启后端** —— 重启后 `is_configured()` 会变为 False (`services/auth.py` 从 auth.json 恢复会话), 面板将回到"未设置密码"状态, 此时**任意内网主机都能通过 `POST /api/auth/setup` 接管面板**。正确顺序是: 先设置新密码, 再重启。
  2. 若 `auth.json` 丢失: 用面板的"设置密码"重新设定, 或用 `AUTH_PASSWORD` 环境变量引导 (`services/auth.py:163-197`)。
  3. 若 `secrets.json` 丢失: 在 设置 → 数据源/AI 中重新填入密钥; 并到供应商侧**轮换**已泄露的 Key。
  4. 若 `api_tokens.json` 丢失: 视为全部 Token 已泄露, 在 设置 → API Token 中**吊销并重建**。
- **预期输出**: 新密码可登录; 旧会话全部失效; 面板可正常拉取行情。
- **失败判断**: 重设密码后仍无法登录 → 检查 `data/user_data` 目录权限与磁盘可写性。
- **停止条件**: 怀疑攻击者仍在面板内 → 断网 (容器 `docker compose stop app`), 更换全部密钥后再恢复。
- **回滚**: 无 (凭据本就应轮换); 若误删, 从配置备份恢复 (注意导入会使会话失效)。

### 2.3 SEV-1 —— 数据损坏

- **诊断**: `POST /api/data/integrity/scan` (`lookback_days` 建议 30 起) → 按报告逐项 `POST /api/data/integrity/repair`。
- **升级条件**: 修复失败或缺口横跨多年 → 只能重新拉取; 确认供应商套餐与密钥仍可覆盖该区间, 否则**数据不可恢复** (这是 HA-001 的实际后果)。
- **停止条件**: 磁盘 < 5% 时停止一切重算。
- **回滚**: 修复走"暂存校验后落盘", 失败不会污染现有分区; 手工改 Parquet **不被允许**。

### 2.4 SEV-2 —— 核心链路不可用

见 `runbook-core-links.md` §2。处置后必须:
1. 记录处置过程 (命令、时间、结果) 到本审计目录的证据区;
2. 若导致数据不一致, 补跑 `POST /api/data/integrity/scan` 确认;
3. 若发生在管道期间, 重跑当日管道并核对 as_of。

### 2.5 事后

- 24 小时内产出事件记录: 时间线、影响面、根因、处置、遗留项。
- 遗留项登记为 `findings.json` 同格式条目, 状态用 `OPEN` / `DEFERRED`。
- 涉及安全的, 同步 `../security/third-party-test-handover.md` 的记录模板。

## 3. 数据备份能力 (目标设计, HA-001 / 批次 C1)

### 3.1 设计要点

1. **与配置备份分离**: 数据快照是独立能力、独立保留策略、独立恢复入口。
2. **原子性**: 复用 `tickflow/repository.py:54-78` 的 `replace_with_retry` 纪律 (临时文件 + 原子替换 + Windows 重试)。
3. **一致性**: 快照必须取在一个**已发布**的 enriched 代际上, 避免抓到"正在发布"的中间态 (`enriched_generation` 的 generation marker 机制即为此设计)。
4. **保留策略**: 按天保留 N 份 + 按周保留 M 份; 快照清单写在 `data_backups/manifest.json`。
5. **异盘**: 同盘快照挡不住磁盘故障 —— 目标路径必须支持异盘 / 可移动介质 / NAS。
6. **默认关闭**: 由偏好开关控制, 默认不启用, 避免在未知磁盘条件下悄悄写满磁盘。

### 3.2 目标命令 (尚未实现, 为目标接口)

```powershell
# 建议形态 (实现前不得写入 Runbook 作为"可用命令")
# 创建快照
POST /api/data/backup/create
# 列出快照
GET  /api/data/backup/list
# 恢复 (需显式确认, 不覆盖除非确认)
POST /api/data/backup/restore  {"backup_id": "...", "confirm": true}
```

### 3.3 恢复演练 (尚未执行, NOT_EXECUTED)

在具备第二环境后按此验证:
1. 在 `data/` 的副本上创建快照;
2. 删除副本的 `data/`;
3. 恢复;
4. 断言 `repo.refresh_cache()` 的 enriched 行数一致, 且抽一个分区比对 sha256;
5. 对比 `GET /api/screener/cached` 的总量与 `/api/pipeline/status` 的 as_of;
6. **记录实际 RTO 与 RPO** —— 本项目当前未定义 RTO/RPO 目标 (任务书为空), 演练前必须先由业务确认。

## 4. 备份当前的临时补偿控制 (HA-001 落地前)

| # | 动作 | 命令 (Windows) | 频率 | 注意 |
| --- | --- | --- | --- | --- |
| 1 | 每日收盘后复制数据目录到异盘 | `robocopy data E:\backup\tsp\$(Get-Date -Format yyyyMMdd) /MIR /R:1 /W:5` | 每个交易日 16:00 后 | 目标盘必须**不在同一物理磁盘**; 备份期间不要跑重算 |
| 2 | 保留最近 7 个交易日 | 手工清理更早的目录 | 每周 | 清理前确认该日数据已在其他备份中存在 |
| 3 | 每月抽检一次可恢复性 | 在临时目录解包/复制一份并打开 | 每月 | **未恢复过 = 不知道能不能恢复** |

> 该补偿控制只能覆盖"人为误删/误格式化", 覆盖不了磁盘硬件故障 (同盘复制) 与勒索软件 (需不可变快照)。
