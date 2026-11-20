# SLI / SLO 设计目标与告警基线（SLI, SLO & Alerts）

> **重要声明**：以下 `99.99%` 等目标为**设计目标（Design Target）**，而非当前单节点架构已达成的生产结论。

## 1. 核心 SLI 指标（已通过 `/metrics` 与 `/health*` 暴露）

| SLI 类别 | 指标 / 探针 | 采集方式 | 建议告警阈值（Staging/生产待校准） |
|---|---|---|---|
| 进程存活 | `GET /health/live` | 容器 `HEALTHCHECK` / 负载均衡探活（30s） | 连续 3 次非 200 → Critical（触发重启） |
| 服务就绪 | `GET /health/ready` | 负载均衡摘流探针（10s） | 返回 503 持续 > 60s → Critical |
| HTTP 错误率 | `tsp_http_requests_total{status="5xx"} / sum(tsp_http_requests_total)` | Prometheus 每 15s 抓取 `/metrics` | 5 分钟窗口 5xx 比例 > 1% → Warning；> 5% → Critical |
| HTTP 时延 | `tsp_http_request_duration_seconds_bucket` | Prometheus histogram_quantile(0.95 / 0.99) | 核心读写 API p95 > 500ms 持续 5m → Warning |
| 在途请求堆积 | `tsp_http_in_flight_requests` | Prometheus 抓取 `/metrics` | 持续 > 80（接近 100 线程池上限）→ Critical |
| 内存压力 | `process_resident_memory_bytes` 及 `/health` `memory.cgroup_events` | Prometheus + `/health` 巡检 | RSS > 容器限额 80% 或 cgroup `high` 持续增长 → Warning/Critical |
| 安全审计异常 | `tsp_audit_events_total{outcome="failed"}` + `data/logs/audit.jsonl` | 日志采集至 SIEM / Loki | 5 分钟内登录失败或配置应用失败激增 → Security Warning |

## 2. 推荐分阶段 SLO
- **阶段 A（当前单节点内网部署）**：月度可用性目标 `99.5%`（允许月停机 ~3.6 小时用于计划升级与单机重启）。
- **阶段 B（状态外置 + 双活后）**：向 `99.99%`（年停机 < 52.6 分钟）演进，需配套真实错误预算与混沌演练验证。
