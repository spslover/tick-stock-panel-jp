# 第三方安全测试交接单（Third-Party Security Test Handover）

- **状态**：`EXTERNAL_REQUIRED`（本仓库未提供第三方渗透测试资料，且本次仅限本地离线验证）

## 1. 建议测试范围（Staging 环境，严禁针对生产）
1. **认证与会话安全**：`/api/auth/setup`、`/api/auth/login`、`/api/auth/change-password` 的暴力破解绕过、会话固定、Cookie 标志（`Secure` 在 HTTPS 反代下生效性）、CSRF 绕过。
2. **Open API 网关越权与限流**：测试 6 类 Scope（`read:market`, `read:ext`, `write:ext`, `read:analysis`, `run:backtest`, `paper:trade`）水平/垂直越权、重复 `Authorization` 头处理、吊销后即时失效、限流绕过。
3. **SSRF 与外部拉取**：外部数据 HTTP Pull、自定义数据源与 AI Provider URL 对私网段/IPv6/DNS Rebinding/302 跳转的拦截有效性。
4. **文件上传与配置备份**：`/api/config-backup/inspect` 与 `/apply` 的 Zip Slip、Zip Bomb、超大文件与恶意内容注入。
5. **信息泄露面**：公网来源访问 `/metrics`、`/docs`、`/openapi.json`、`/health` 是否按预期拦截或脱敏。

## 2. 测试前提与证据要求
- 必须使用独立合成数据与临时测试凭据，禁止使用真实交易或生产密钥。
- 渗透测试报告须包含复现步骤、CVSS 评分、受影响路由与复测闭环结论，归档至本目录后方可更新发布门禁状态。
