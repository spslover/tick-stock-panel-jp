# 安全修复与扫描汇总（Security Fixes & Scan Summary）

## 1. 安全控制与扫描执行矩阵

| 检查类别 | 工具 / 方法 | 执行状态 | 结果摘要 |
|---|---|---|---|
| 认证、会话、CSRF、网关边界回归 | `pytest tests/test_m1_edge_auth_security.py` | `VERIFIED` | 全量通过 |
| SSRF、跨源 Cookie 剥离、密钥脱敏回归 | `pytest tests/test_m1_ssrf_secrets.py` | `VERIFIED` | 全量通过 |
| 路径穿越、模拟盘落盘、基准缓存并发回归 | `pytest tests/test_m2_1_hardening.py` | `VERIFIED` | 全量通过（修正 2 处基线断言并增强 `%2e%2e` 校验） |
| 可观测性端点权限、Request-ID 防注入、审计脱敏 | `pytest tests/test_audit_observability_hardening.py` | `VERIFIED` | 4/4 PASSED（含公网 IP 访问 `/metrics` 拒绝验证） |
| 后端静态缺陷检查 (ruff critical) | `ruff check --select F821,F811,RUF006 app/ tests/` | `VERIFIED` | 0 错误（修复 1 `F821`, 1 `F811`, 6 `RUF006`） |
| 前端静态安全与质量检查 (eslint) | `corepack pnpm eslint --quiet .` | `VERIFIED` | 0 错误（修复 5 Hook 崩溃 + 3 语法/未使用项） |
| Python/JS 依赖 CVE 扫描 (`pip-audit`, `pnpm audit`) | — | `NOT_EXECUTED` | `ALLOW_NETWORK=false`，离线无漏洞库 |
| 容器镜像漏洞扫描 (`trivy` / `grype`) | — | `NOT_EXECUTED` | 未在本地构建容器镜像且无外网 |
| 第三方渗透测试 / DAST | — | `EXTERNAL_REQUIRED` | 未提供授权与第三方测试报告 |

## 2. 监管与合规映射说明（`NEEDS_COMPLIANCE_REVIEW`）

因本次未提供 `JURISDICTION` 与 `REGULATORY_BASELINE`，本报告**不宣称符合任何特定国家或地区的金融监管法规**。通用工程控制与常见审计关注点映射如下（待法务/合规团队确认）：
- **访问控制与最小权限**：已实现密码认证、6 级 API Token Scope、默认内网门禁（`IMPLEMENTED` / `VERIFIED`）。
- **审计留痕与防篡改**：已实现本地脱敏 `audit.jsonl`（`VERIFIED`）；集中式不可变归档与法定保留期限（如 5 年/7 年）需运维与合规补齐（`NEEDS_COMPLIANCE_REVIEW`）。
- **传输加密与密钥轮换**：后端自身为 HTTP，生产必须前置 TLS 1.2+/1.3 反向代理并建立定期密钥轮换流程（`EXTERNAL_REQUIRED`）。
