# 安全 — 第三方渗透测试交接材料

审计编号 AUDIT-2026-11-20-001 | 代码版本 `8cd1010` | 日期 2026-11-20
状态: **EXTERNAL_REQUIRED** —— 未提供授权范围与资料, 本审计**未**执行任何渗透测试。

## 1. 授权范围 (需由委托方填写)

```text
被测目标:        ________________________________________________
版本/提交:       v0.3.37 / 8cd101035b1cd3cf995a3548de51a9b87c927514
测试窗口:        ________________________________________________
允许的时间:      ________________________________________________
来源 IP 段:      ________________________________________________
允许的测试类型:  □ 外部黑盒  □ 内网黑盒  □ 灰盒(提供测试账号)
                 □ 代码审计复核  □ API 契约测试
明确禁止:        □ 生产环境   □ 社会工程   □ DoS/压力测试
                 □ 第三方供应商系统   □ 数据销毁
应急联系人:      ________________________________________________
```

## 2. 被测架构 (脱敏说明, 可直接发给第三方)

**产品形态**: 单用户、自托管的本地优先 A 股行情分析面板。后端 FastAPI + Polars/DuckDB/Parquet, 前端 React + Vite。数据源为可插拔的 HTTP 数据源 (用户自建) 或内置 TickFlow SDK。

**网络拓扑**:
```text
测试客户端 ──HTTP(S)──> [FastAPI 单实例]
                        ├─ 仅监听 127.0.0.1 或内网地址
                        ├─ 鉴权: 单访问密码会话 (HttpOnly Cookie) 或 Bearer API Token
                        ├─ 数据出口: 用户配置的 HTTP 数据源 / TickFlow 云端
                        └─ 本地磁盘: data/ (凭据 + 行情 + 策略代码 + 页面包)
```

**需要外部可见性的攻击面**:
1. `/api/*` (受会话或 Token 保护; `/api/auth/*`、`/health`、`/api/health`、`/api/openapi.json`、`/api/events` 在鉴权白名单内)
2. `/api/openapi.json?tier=a` —— 对外契约视图 (Token 可达端点)
3. SSE `/api/events` (一次性票据)、`/api/intraday/stream`
4. 静态 SPA 与运行时页面包 (`/api/ext-pages/{pack_id}/bundle.js`)
5. 文件类端点: 扩展数据上传/导入、配置备份导出/导入、自选股 CSV/图片导入

**信任边界**: 见 `threat-model.md` §2 (TB1..TB6)。

## 3. 测试准备清单 (需在开测前完成)

| # | 项目 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | 使用**合成数据**, 不使用真实账户数据 | 待办 | 在 `DATA_DIR` 放一份构造的 `data/` (含假的 instruments/parquet/shares), 不含任何真实密钥 |
| 2 | 设置测试密码与专用 API Token | 待办 | 通过 `/api/auth/setup` 设置; Token 用最小 scope |
| 3 | 记录基线哈希以便事后确认未被破坏 | 待办 | `Get-FileHash` 覆盖 `data/user_data/*` 与 `data/strategies/*` |
| 4 | 关闭对外暴露 | 待办 | 绑定 127.0.0.1, 用 SSH 端口转发访问 (见 `docs/deployment.md`) |
| 5 | 确认没有真实 `.env` / 凭据 | 待办 | `data/user_data/secrets.json` 置空或填假值 |
| 6 | 约定数据清理方式 | 待办 | 测试后整体删除 `DATA_DIR` (属测试方自建的目录) |

## 4. 重点测试项 (基于本次审计的静态发现排序)

| 优先级 | 测试项 | 关联发现 | 期望结果 |
| --- | --- | --- | --- |
| 1 | 所有路径参数型端点的目录穿越 (Windows 与 Linux 两种分隔符) | SEC-001 (已修复), 需验证其余端点 | 一律 400/404, 不得触碰目录外文件 |
| 2 | 归档导入的路径穿越 / zip bomb / 伪造中央目录 | config_backup 白名单已存在, 需验证 | 拒绝且不落盘 |
| 3 | 会话与 Token 通道的越权 (scope 边界) | api_gateway 六档 scope | Token 不可达非授权路径; 未授权返回统一拒绝 |
| 4 | CSRF / 跨源状态变更 | SEC-005 (双提交分支不可达) | 跨源不安全请求被拒 (依赖 Origin 校验) |
| 5 | `/api/auth/setup` 的未认证可达性 | SEC-003 | 未设密码时的内网可达性行为与设计一致 |
| 6 | 页面包 / 扩展包的能力边界 | SEC-007 / FE-004 | 明确记录可达成的能力 (本审计认为无边界, 属设计事实而非漏洞) |
| 7 | 凭据导出接口的可达性与脱敏 | `/api/config-backup/export?categories=secrets` | 仅认证后可达; 响应脱敏符合预期 |
| 8 | SSRF: 数据源 URL 指向内网 / 元数据地址 | `safe_http` 已阻断 | 被阻断并有清晰错误 |
| 9 | 请求体上限与并发上限 | `BodyLimitMiddleware` / `heavy_job_limiter` | 超限返回 413; 并发重任务返回 503 |
| 10 | 审计与可追溯性 | SEC-002 | 当前**不满足** (无审计落点) —— 记录为观察项而非缺陷判定 |

## 5. 结果导入模板

```yaml
engagement:
  vendor: ____________________
  tester: ____________________
  started_at: ____________________
  finished_at: ____________________
  target_version: v0.3.37
  target_commit: 8cd101035b1cd3cf995a3548de51a9b87c927514
  scope_confirmed: true/false
findings:
  - id: TP-001
    title: ____________________
    severity: Critical|High|Medium|Low
    cvss: ____________________
    affected_endpoint: ____________________
    affected_component: ____________________
    reproduction_steps: |
      ____________________
    observed_result: |
      ____________________
    expected_result: |
      ____________________
    evidence_refs: [____________________]
    retest_status: NOT_RETESTED|VERIFIED_FIXED|STILL_PRESENT
    mapped_finding_id: SEC-___        # 若与本审计清单对应
    status: OPEN|ACCEPTED|FIXED|DEFERRED
    compensating_control: ____________________
    owner: ____________________
    due_date: ____________________
```

## 6. 交接后的处理流程

1. 收到报告后 3 个工作日内完成去重: 与 `findings.json` 的 65 项交叉比对, 标注 `mapped_finding_id`; 本审计已修复项 (SEC-001) 若被复测发现, 立即升级为 P0 并回滚相关发布。
2. 新发现按严重度映射优先级 (P0 立即处置 / P1 当前周期 / P2 下一迭代 / P3 纳入计划)。
3. 每条发现必须补齐: 证据、复现步骤、修复方案、验证方法、回滚方式、残余风险 —— 与本审计的字段契约一致。
4. 复测结果回填 `04-remediation-ledger.md` 的同类台账。
5. **不得**以第三方报告替代本次审计的本地验证结论, 也不得据此宣称"符合监管要求" —— 法域与规范仍未提供 (NEEDS_COMPLIANCE_REVIEW)。
