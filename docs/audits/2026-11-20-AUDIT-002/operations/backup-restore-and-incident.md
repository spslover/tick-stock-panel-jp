# 备份恢复与应急演练规范（Backup, Restore & Incident）

- **演练执行状态**：`NOT_EXECUTED`（受 `ALLOW_DESTRUCTIVE_TEST=false` 与 `TARGET_ENV=local` 限制，以下为标准操作规程，上线前必须在 staging 完成计时演练并记录实测 RTO/RPO）

## 1. 数据资产与备份范围

| 目录 / 文件 | 内容 | 推荐备份频率 | 备份方式 |
|---|---|---|---|
| `data/user_data/` | 密码哈希、API Token 哈希、自选、策略、偏好设置 | 每日 + 重大变更前 | 加密归档或 `/api/config-backup/export`（不含明文密钥） |
| `data/paper/` | 模拟盘账户、委托、持仓、执行租约 SQLite | 每交易日盘后 | 停写窗口内执行 SQLite `.backup` + 目录快照 |
| `data/logs/audit.jsonl` | 安全审计日志 | 每日归档 + 实时采集 | 同步至独立只读日志存储/SIEM，禁止就地截断删除 |
| `data/kline_*`, `data/adj_factor*`, `data/instruments*` | Parquet 行情湖仓 | 每周全量 + 每日增量 | 文件系统快照（LVM/ZFS/云盘快照）或 `rsync` 至异地对象存储 |

## 2. 标准恢复流程（Staging 演练步骤）
1. 停止应用服务，确保无后台任务写入 `data/`。
2. 将目标时间点快照恢复至干净目录 `data_restore/`，校验关键 JSON 与 Parquet 文件可读性。
3. 切换 `DATA_DIR` 指向恢复目录并启动服务，验证：
   - `GET /health/live` 与 `GET /health/ready` 返回 `200`；
   - 模拟盘账户/持仓余额与快照前一致；
   - `audit.jsonl` 历史记录完整且新操作可正常追加。
4. 记录实测 **RTO**（停服到 `/health/ready` 恢复 200 的总耗时）与 **RPO**（快照时间与故障时间的差值），补充回填至本报告。
