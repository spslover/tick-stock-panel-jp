# 安全 — 威胁模型

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20
适用产品: Tick Stock Panel (单用户自托管 A 股行情分析面板) | 部署形态: 本机 / Docker / 桌面版 (PyInstaller)

## 1. 资产分类

| 等级 | 资产 | 位置 | 备注 |
| --- | --- | --- | --- |
| **A 最高** | TickFlow API Key、AI Provider Key、Webhook 密钥、SMTP 口令 | `data/user_data/secrets.json` (0600) | 泄露即等于账户被接管; 响应中一律掩码 (`api/settings.py:133,148`) |
| **A** | 访问密码哈希与盐、会话令牌 | `data/user_data/auth.json` | PBKDF2-HMAC-SHA256 / 200k 轮 / 16B 盐 / 常量时间比较 |
| **A** | API Token | `data/user_data/api_tokens.json` | **只存 SHA-256 摘要**, 明文仅创建时返回一次 (`services/api_tokens.py:128-184`) |
| **B** | 自定义策略代码 (可执行 Python) | `data/strategies/custom` | AST 校验 + 受限 builtins + 子进程沙箱 (`strategy/sandbox_runner.py:1201-1247`) |
| **B** | 页面包 / 扩展包 (可执行 JS 与 Python) | `data/**/page-packs`、`backend/app/plugins` | 同源执行, 无能力边界 (FE-004 / SEC-007) |
| **B** | 历史行情、enriched 指标、财务数据 | `data/kline_*`、`data/financials` | **唯一系统记录, 无备份** (HA-001) |
| **C** | 自选股、监控规则、告警、模拟盘账户 | `data/user_data`、`data/paper` | 业务数据, 泄露影响有限 |
| **C** | 访问日志 | `data/logs/backend.log` (3 个轮转文件) | 无关联 ID、无防篡改、无访问控制 |

## 2. 信任边界与攻击面

```text
[浏览器] --Cookie/无 Token--> [FastAPI] --HTTPS--> [供应商]
   |                            |
   | 同源 JS                    | + 本地磁盘 (唯一系统记录)
   v                            v
[页面包/扩展]              [data/ 凭据 + 数据 + 策略代码]
   ^ 可读全部 /api/ (无能力边界)
```

| 编号 | 边界 | 攻击者 | 可达能力 |
| --- | --- | --- | --- |
| TB1 | 浏览器 ↔ API | 已登录用户 / 内网未认证主机 | 全部 `/api/` (单密码单用户模型, 无角色) |
| TB2 | API ↔ Token 通道 | 持有 Token 的外部调用方 | 六档 scope, deny-by-default, 限流 |
| TB3 | API ↔ 外部 HTTP | 被配置为数据源的地址 | `safe_http` 的 SSRF 防护 (scheme 白名单 + 私网/链路本地阻断 + DNS 解析 + 重定向剥离凭据) |
| TB4 | API ↔ 磁盘 | 路径参数 / 归档 | **本次修复了唯一确认实例 (SEC-001)** |
| TB5 | 插件 / 页面包 ↔ 宿主 | 插件作者或页面包作者 | 插件: 加载隔离; 页面包: **无能力边界** |
| TB6 | 归档 ↔ 导入 | 构造的备份包 | 无 `extractall`, 路径白名单 + 体积上限 + 截断读取 + 导入后重新 AST 校验 |

## 3. 威胁 (STRIDE 摘要)

| 威胁 | 场景 | 现有控制 | 残余风险 |
| --- | --- | --- | --- |
| **Spoofing** | 内网主机在未设密码时抢先认领面板 | `_is_local_network` 限制 | **SEC-003 (P1)**: RFC1918 全段视为可信, 无一次性认领码 |
| **Spoofing** | 会话 Cookie 被 LAN 嗅探重放 | HttpOnly + SameSite=Lax | **SEC-004**: 明文 HTTP 下无 `Secure` 标志 |
| **Tampering** | 路径参数穿越删除/覆盖文件 | 多数端点有白名单+包含检查 | **SEC-001 (已修复)**; 其余端点未穷尽审计 |
| **Tampering** | 构造归档导入 | 路径白名单 + 体积上限 + 导入后 AST 复验 | 低 |
| **Repudiation** | 无法追溯凭据变更、Token 签发、密钥导出 | 仅普通日志 | **SEC-002 (P2)**: 无安全审计落点, 无关联 ID |
| **Information disclosure** | 页面包读取凭据导出接口 | — | **SEC-007 / FE-004**: 同源 + 完整 api 封装 |
| **Information disclosure** | 备份包含可重放会话 | auth.json 为 0600, 但备份导出包含它 | **SEC-006 (P3)** |
| **Denial of service** | 单次请求体过大 / 批量重活占满线程池 | `BodyLimitMiddleware` (按路径分级) + `heavy_job_limiter` + `workload` 准入 | 全局 body 上限 10MB; 内网可绕过部分准入 |
| **Denial of service** | 自定义源 URL 指向内网 / 元数据服务 | `safe_http` 阻断私网与链路本地 | `global_market_data.py` 使用**硬编码常量主机**, 绕开该封装 (PERF-003) |
| **Elevation** | 插件/页面包获得宿主能力 | 插件加载隔离; 代码执行端点额外要求同源 + 会话 | 页面包无能力边界 |

## 4. 已验证的安全控制 (本次审计复核通过)

1. 密码存储: PBKDF2-HMAC-SHA256 / 200000 轮 / 16 字节随机盐 / `secrets.compare_digest` (`services/auth.py:30,82-98`)
2. 改密使全部会话失效 (内存 + 磁盘) (`services/auth.py:152-158`, `api/auth.py:218`)
3. 登录竞态: 会话铸造前在锁内重读并比对密码哈希 (`services/auth.py:211-216`)
4. 登录限流: 持久化、有界 (1024 条)、按可信代理感知的客户端 IP (`services/auth.py:318-374`)
5. XFF 只在 socket peer 属于 `TRUSTED_PROXY_CIDRS` 时才采信 (`config.py:247`)
6. API Token 只存 SHA-256 摘要; 明文仅一次; 列表视图不含 `token_hash`; 校验用 `hmac.compare_digest`
7. 六档 scope, 无 admin scope, 未匹配路径默认拒绝
8. 令牌网络门禁先于令牌校验执行 → 公网探测拿不到"令牌是否存在"的信号, 也不消耗限流额度
9. 网关 scope 匹配器本身拒绝 `..`、`%2f`、`%5c`、`%00`、`//`、`\` 形态的路径
10. 代码执行端点额外要求: 拒绝 `Authorization` 头 + 同源 + 拒绝 `Sec-Fetch-Site: cross-site` + 有效会话
11. 策略删除 fail-closed, 且校验目标位于用户策略目录内 (`api/strategy.py:1432-1438`)
12. 归档处理不含 `extractall`; 条目按路径白名单分类, 逐文件/逐包/总量上限, 截断读取以挫败伪造的中央目录
13. 导入的策略/持仓策略代码会重新走引擎的 AST 安全校验 (不信任"自己导出的"归档)
14. secrets 存 0600, 默认导出不含 secrets (仅显式 "secrets" 类别包含), 响应中掩码
15. 上游错误在返回前擦除内部路径与疑似密钥文本; 含密钥的 URL 在日志中被脱敏
16. SSRF 防护: scheme 白名单 + 私网/链路本地阻断 (169.254.0.0/16 明确排除) + DNS 解析
17. CORS 默认拒绝全部; `allow_credentials` 恒为 False; 通配 origin 强制关闭凭据
18. 安全响应头 + 按路径的 CSP (内联脚本按哈希放行)
19. 全局请求体上限 (按路径分级), 同时校验 Content-Length 与流式分片
20. CSRF: `/api/` 下的不安全方法要求同源 Origin/Referer 且拒绝跨站 `Sec-Fetch-Site`
21. 前端产物不含秘密: 唯一 `import.meta.env` 使用点是 `BASE_URL`; 会话为 HttpOnly Cookie, 前端不存 Token; `sourcemap: false`
22. 通知子进程调用使用参数列表, 无 `shell=True`

## 5. 本次安全修复

**SEC-001** (P0, IMPLEMENTED/VERIFIED): 路径穿越 → 任意 .json 删除。漏洞在审计中被**实际复现** (`{"status":"deleted"}` + `secrets.json` 被删除)。修复后 7 个回归用例全绿。详见 `../04-remediation-ledger.md` §1。

## 6. 扫描结果摘要

| 扫描 | 工具 | 结果 | 状态 |
| --- | --- | --- | --- |
| 后端静态检查 | ruff (仓内) | 214 BLE001 / 42 I001 / 37 RUF100 / 31 F401 / 16 B023 … | **已执行** (`evidence/ruff-baseline.txt`) |
| 前端静态检查 | eslint 9.39.5 (仓内) | 8 errors / 227 warnings | **已执行** (`evidence/eslint-baseline.txt`) |
| 依赖 CVE (pip) | pip-audit | — | **NOT_EXECUTED** (未安装, 不允许联网) |
| 依赖 CVE (npm) | pnpm audit | — | **NOT_EXECUTED** |
| 容器镜像 | Trivy / Grype | — | **NOT_EXECUTED** |
| IaC / 配置 | Checkov / kube-bench | — | **NOT_EXECUTED** (本项目无 K8s IaC) |
| 秘密扫描 | gitleaks | — | **NOT_EXECUTED**; **本次人工检查**: 未在 `backend/app`、`frontend/src`、`docs/` 中发现硬编码密钥; 秘密统一存于 `data/user_data/secrets.json` 且被 `.gitignore` 覆盖 |
| SAST | CodeQL / Semgrep | — | **NOT_EXECUTED** |

> 上述未执行项的落地路径见 `../03-remediation-plan.md` 批次 B5。

## 7. 合规映射状态

**NEEDS_COMPLIANCE_REVIEW** —— JURISDICTION 与 REGULATORY_BASELINE 均未提供。
本审计仅完成**通用工程控制审查** (§4 的 22 项), **未**将任何发现映射到具体监管条款, **未**作出合规断言。若后续提供法域与规范, 需由合规负责人完成映射, 并对 §4 逐条给出"满足/不适用/需补偿控制"的判定。
