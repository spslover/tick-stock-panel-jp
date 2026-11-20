# 安全威胁模型与数据分级（Threat Model）

## 1. 数据分类分级（基于代码识别）

| 级别 | 数据类别 | 存储位置 / 传输通道 | 保护现状 |
|---|---|---|---|
| L4 极敏感 | 访问密码哈希、会话令牌、API Token 明文/哈希、第三方数据源 API Key、AI 模型 API Key | `data/user_data/auth.json`, `api_tokens.json`, `.env`, Cookie (`tf_session`), HTTP `Authorization: Bearer` | 密码 scrypt 哈希、Token SHA-256 哈希、Cookie `HttpOnly`+`SameSite=Lax`、`audit.jsonl` 强制脱敏 |
| L3 敏感 | 模拟盘账户/委托/持仓、自选股、自定义策略与监控规则、配置备份 ZIP | `data/paper/`, `data/user_data/`, `/api/config-backup/*` | 需要登录会话或对应 API Token scope；配置备份默认不含密钥（需显式 `categories=secrets`） |
| L2 内部 | 内部运行指标、拓扑与诊断日志 | `/metrics`, `/health`, `data/logs/audit.jsonl` | `/metrics` 已限制仅本机/内网或已认证会话访问；`/docs` 已受认证/内网门控 |
| L1 公开/半公开 | 市场行情 K 线、公开指数、前端静态资源 | `data/kline_*`, `/assets/*` | API 访问受认证/Token 网关控制 |

## 2. STRIDE 威胁分析（核心边界）

| 威胁类型 | 攻击面 | 已有控制 | 本次加固 / 残余缺口 |
|---|---|---|---|
| **S 伪装 (Spoofing)** | 公网抢占首次设密码、弱密码暴力破解、伪造 `X-Forwarded-For` | `/api/auth/setup` 限本机/内网；登录 IP 失败限流；`TRUST_PROXY` 门控 | 已在 `auth.py` 接入成功/失败审计留痕；公网部署前仍需反代层二次限流 |
| **T 篡改 (Tampering)** | CSRF 跨站写操作、路径穿越删除模拟盘订单、CRLF 日志注入 | `_verify_csrf` 同源校验；`paper.py` ID 白名单校验；`sanitize_request_id` 正则清洗 | `audit.jsonl` 目前为本地文件追加写，尚未接入远端 WORM/SIEM 防篡改（`NEEDS_COMPLIANCE_REVIEW`） |
| **R 抵赖 (Repudiation)** | 密码变更、API Token 签发/吊销、配置导入导出无迹可查 | 原有仅零散 INFO 日志 | **已修复（`VERIFIED`）**：统一写入结构化脱敏 `data/logs/audit.jsonl` 并关联 `X-Request-ID` |
| **I 信息泄露 (Info Disclosure)** | `/metrics` 暴露内存与接口频次、`/docs` 暴露内部接口、SSRF 探测内网 | `_DOCS_PATHS` 认证门控；`safe_http.py` 禁内网 IP 与跨源剥离 Cookie | **已修复（`VERIFIED`）**：`/metrics` 增加本机/内网或会话认证门控；`audit.jsonl` 脱敏过滤 |
| **D 拒绝服务 (DoS)** | 大报文耗尽内存、高频 Open API 请求、Polars 并发死锁、悬空任务泄露 | `BodyLimitMiddleware`、Open API 令牌桶限流、`polars_guard`、`HealthWatchdog` | **已修复（`VERIFIED`）**：消除 6 处悬空任务；缺分布式限流与 WAF（`EXTERNAL_REQUIRED`） |
| **E 权限提升 (EoP)** | Bearer Token 越权访问管理接口、容器 root 运行 | 6 级 scope 白名单（禁止管理/认证端点）；`API_TOKEN_LOCAL_ONLY` 默认开启 | 容器 root 隔离待结合卷权限迁移（`OPS-CONTAINER-001`, `PARTIAL`） |
