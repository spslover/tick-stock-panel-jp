# 核心链路运维手册（Runbook - Core Links）

## 1. 服务启动与健康核查
1. **探活顺序**：
   - 存活：`curl -sf http://127.0.0.1:3018/health/live` → 期望 `200 {"status":"ok"}`
   - 就绪：`curl -sf http://127.0.0.1:3018/health/ready` → 期望 `200 {"status":"ready", "checks":{"repo":true,"data_dir":true,"indicators_ready":true}}`
   - 指标：`curl -sf http://127.0.0.1:3018/metrics | head -n 20`（仅本机/内网或携带有效登录 Cookie）
2. **全链路排障**：每个 HTTP 响应均携带 `X-Request-ID`，排查安全/写操作故障时使用该 ID 在应用日志与 `data/logs/audit.jsonl` 交叉检索。

## 2. 常见故障处置

### 场景 A：`/health/ready` 返回 `503`
- 检查响应体 `checks` 字段：
  - `data_dir: false`：检查宿主机卷挂载 `./data:/app/data` 权限与磁盘空间（`df -h`）。
  - `indicators_ready: false`：检查启动日志是否正在执行大批量指标预热或 Parquet 视图修复；若持续超时，重启容器并保留日志。

### 场景 B：`HealthWatchdog` 触发退出码 `70`
- **现象**：日志出现 `watchdog: backend wedged ... exiting for supervisor restart`。
- **处置**：Docker `restart: unless-stopped` 将自动拉起容器；事后立即采集 `/health` 内存快照与最近 5 分钟 `audit.jsonl` / 应用日志，排查是否存在并发大查询或 `polars_guard` 阻塞。

### 场景 C：登录/API Token 异常或疑似凭据泄露
1. 检索 `data/logs/audit.jsonl` 中 `action` 为 `auth.*` 与 `api_token.*` 的记录（含 `client_ip` 与 `request_id`）。
2. 立即吊销涉事 Token（`DELETE /api/settings/api-tokens/{token_id}`）并执行改密（`/api/auth/change-password`，自动清空全部历史会话）。
3. 确认生产部署已在 `.env` 设置 `HOST=127.0.0.1` 且所有外部流量经由 TLS 反向代理进入。
