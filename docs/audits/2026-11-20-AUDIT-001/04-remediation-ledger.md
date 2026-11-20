# 04 — 修复台账 (Remediation Ledger)

审计编号 AUDIT-2026-11-20-001 | 基线代码版本 `8cd1010` | 日期 2026-11-20
本台账逐条记录本次审计**实际实施**的变更: 问题编号 → 代码变更 → 测试 → 证据 → 状态 → 残余风险。

## 变更总览

```text
 .github/workflows/ci.yml                      |  3 +++
 backend/app/api/analysis.py                   | 25 ++++++--
 backend/app/data_providers/custom/provider.py | 43 ++++++++-------
 backend/app/share_capital.py                  | 10 +++-
 backend/tests/test_custom_pct_units.py        | 77 ++++++++++++++++-----
 backend/tests/test_financial_shares.py        | 51 +++++++++++++-
 docs/custom-data-source.md                   |   4 +-
 frontend/src/pages/Indices.tsx                | 13 ++++-
 8 files changed, 181 insertions(+), 45 deletions(-)

新增文件:
 backend/tests/test_analysis_menu_path_traversal.py   (SEC-001 回归, 7 用例)
 frontend/src/pages/Indices.symbolSwitch.test.tsx     (FE-01 回归, 1 用例)
```

> 以上 8 个被修改的文件在审计开始时**均未被修改** (工作区中已有的他人改动集中在其他前端文件、`docs/features.md` 与 `操作说明书.md`), 因此本台账的每一处 diff 都来自本次审计, 未覆盖任何既有改动。

---

## 1. SEC-001 — 分析菜单删除接口路径穿越 (P0)

| 项 | 内容 |
| --- | --- |
| 问题编号 | SEC-001 |
| 维度 | security |
| 严重度 / 优先级 | High / P0 |
| 状态 | CONFIRMED → **IMPLEMENTED / VERIFIED** |
| 受影响组件 | `backend/app/api/analysis.py` |

**根因 (VERIFIED)**: 写入侧 `upsert_menu` 有 `menu_id.replace("_","").isalnum()` 校验 (analysis.py:182-183), 删除侧 `delete_menu` 没有任何校验, `_path()` 直接把 `menu_id` 拼进文件路径后 `unlink()`。两侧契约不一致是根因 —— 这类"逐端点手工添加校验"的模式必然遗漏。

**代码变更** (`backend/app/api/analysis.py`):
1. 新增模块级 `_MENU_ID_RE = re.compile(r"^[a-zA-Z0-9_]+$")` —— 与 `AnalysisMenu.id` 的 pattern 一致 (ASCII 字母/数字/下划线)。
2. 新增 `_validate_menu_id(menu_id)` —— 统一抛 400, 供写入与删除共用。
3. `_path()` 内先校验标识符, 再做**目录包含检查**: `p = (base / f"{menu_id}.json").resolve()`, 要求 `p.parent == base.resolve()`, 否则 400 "非法路径"。该范式与 `backend/app/api/pages_admin.py:47-58` 一致。
4. `upsert_menu` 改为调用 `_validate_menu_id(menu_id)` (去掉重复的内联 `isalnum()` 判断)。
5. 新增 `import re`。

**回归测试** (新增 `backend/tests/test_analysis_menu_path_traversal.py`, 7 用例):
- `test_delete_rejects_backslash_traversal_and_keeps_target` — `..%5Cuser_data%5Csecrets` (Windows 可利用的实际载荷) 必须 400 且 `secrets.json` 仍存在
- `test_delete_rejects_forward_slash_traversal_and_keeps_target` — `..%2F..%2Fuser_data%2Fsecrets`
- `test_delete_rejects_dot_prefix_traversal_and_keeps_target`
- `test_delete_rejects_absolute_path_traversal` — `C:%5CWindows%5Csystem.ini`
- `test_normal_delete_still_works` / `test_delete_missing_menu_returns_404` / `test_upsert_rejects_traversal_id` — 正常路径与写入侧契约不受影响

**证据 (修复前 → 修复后)**:
- 修复前: `4 failed, 3 passed`, 失败信息 `AssertionError: {"status":"deleted"} / assert 200 == 400` —— **漏洞被实际复现, `data/user_data/secrets.json` 被真实删除**
- 修复后: `10 passed` (含既有的 `test_analysis_menu_url_template.py` 5 用例), 0.91s

**回滚方式**: 回滚 `backend/app/api/analysis.py` 即可 (原行为即漏洞本身, 回滚会重新引入风险, 因此不建议)。
**残余风险**: 仅 `analysis.py` 一处命中同类 `_path` 模式; 其余 API 端点的路径参数未做穷尽审计 → 见 §"延期项"。

---

## 2. DATA-001 — 自定义实时源按数值猜涨跌幅单位 (P0)

| 项 | 内容 |
| --- | --- |
| 问题编号 | DATA-001 |
| 维度 | data_consistency |
| 严重度 / 优先级 | High / P0 |
| 状态 | CONFIRMED → **IMPLEMENTED / VERIFIED** |
| 受影响组件 | `backend/app/data_providers/custom/provider.py`、`docs/custom-data-source.md` |

**根因 (VERIFIED)**: `_normalize_pct_units` 在未声明 `pct_unit` 时按 `|change_pct|` 中位数(样本 ≥5)或最大值(样本 <5)是否超过 `_PCT_FRACTION_MAX = 0.31` 判定整批是否 /100。这与 CONTRIBUTING §3.1 的明令禁止直接冲突, 且函数自己的 docstring 写着"单位只认显式声明, 不靠数值猜" —— 代码与自身契约矛盾。文档 `docs/custom-data-source.md` 内部亦矛盾 (:138 说不做数值猜测, :153 说按截面中位数归一)。
可复现的失败场景: 百分制源 + 平静行情(中位数 ≤ 0.31)或小样本(最大 ≤ 0.31) → `0.05`(=0.05%) 被当成小数制 `5%`, **静默 100 倍错误**。

**代码变更** (`backend/app/data_providers/custom/provider.py`):
1. **删除** `_PCT_FRACTION_MAX` 常量及其"物理上限"注释 —— 数值推断不再存在。
2. **删除** 按中位数/最大值判定的分支; 未声明单位时, 三列一律先置 `None` (fail-closed), 与既有的 amplitude/turnover_rate 处置一致。
3. **新增** `change_pct` 的价格重算: `(last_price - prev_close) / prev_close`。**价格比值与源端单位无关, 恒为小数制** —— 这是在不做任何单位猜测的前提下得到正确值的唯一可靠途径; 缺 `last_price`/`prev_close` 或 `prev_close == 0` 时保持 `None`。
4. 更新 docstring 与 WARNING 文案, 明确三列的处置。

**测试变更** (`backend/tests/test_custom_pct_units.py`, 21 用例全绿):
- 更新模块 docstring 为新契约;
- `test_undeclared_change_pct_is_not_scaled_by_value` (原 `..._percent_batch_normalized`) —— 断言不再出现任何被 /100 缩放的数值;
- `test_limit_up_fraction_30cm_not_divided` —— 30cm 极值不再被数值判定;
- `test_small_batch_is_not_guessed` (原 `test_small_batch_uses_max`) —— 小样本不再用最大值判定;
- `test_string_values_are_cast` —— 断言仍被转 Float64 且声明 percent 时换算正确;
- `test_undeclared_amplitude_and_turnover_are_nulled` —— change_pct 缺价格时同样 fail-closed;
- `test_get_realtime_undeclared_nulls_ambiguous_columns` —— provider 集成层断言 change_pct 由价格重算 (`S1 ≈ 0.015228`, `S2 ≈ -0.021526`);
- **新增** `test_undeclared_change_pct_recomputed_from_prices`、`test_undeclared_change_pct_never_hundredfold_scaled` (显式回归: 百分制源不得出现 100 倍错值)、`test_undeclared_change_pct_null_without_prices`。

**文档变更** (`docs/custom-data-source.md`): 未声明行改为描述"不做任何按数值的推断 + change_pct 由价格重算 + 缺价格置 None"; `pct_unit: percent` 注释中去掉"或省略"。

**证据**: 修复后 `21 passed` (0.88s)。
**回滚方式**: 回滚 `provider.py`、`test_custom_pct_units.py` 与文档三处 (三者必须同步回滚, 否则契约与实现再次分叉)。
**残余风险**: 依赖旧启发式的自定义源将从"静默错值"变为"按价格重算或空值" → **必须在发版说明中提示用户显式声明 `pct_unit`**。

---

## 3. DATA-002 — 历史股本报表期泄露 (P1)

| 项 | 内容 |
| --- | --- |
| 问题编号 | DATA-002 |
| 维度 | data_consistency |
| 严重度 / 优先级 | High / P1 |
| 状态 | CONFIRMED → **IMPLEMENTED / VERIFIED** |
| 受影响组件 | `backend/app/share_capital.py` |

**根因 (VERIFIED)**: `available_date = as_date_expr("period_end")`, 有 `announce_date` 列时再 `.fill_null(available_date)` —— 公告日为空即按报表期生效, 等于断言"财报在报告期末已公开"。CONTRIBUTING §3.4 明确"报表期不得泄露尚未公告的数据"。

**代码变更** (`backend/app/share_capital.py`):
- 可用日只认 `announce_date`: `available_date = as_date_expr("announce_date")`;
- **整表无 `announce_date` 列时**同样置为不可用 (`pl.lit(None, dtype=pl.Date)`) —— 公告时间不可知即不可用于历史;
- 既有的 `.filter(pl.col("_share_available_date").is_not_null())` 会把这些行排除出历史表, 由既有的 `pl.coalesce("_historical_float_shares", "float_shares")` 完成 §3.4 允许的"最新维表股本"降级 (该降级路径本就存在, 未新增逻辑);
- docstring 补充该契约。

**测试变更** (`backend/tests/test_financial_shares.py`):
- 更新既有用例 `test_historical_turnover_uses_only_available_share_capital` 的期望值: `[0.5, 0.5, 1.0, 2.0, 0.5]` → `[0.5, 0.5, 1.0, 1.0, 0.5]`。**原期望值中的 2.0 正是泄露本身** (announce_date=None 的行在 period_end 当天被当作可用); 改后该日沿用上一条已公告的 1 亿股。断言未放宽, 反而新增了"不得出现 2.0"的显式说明。
- **新增** `test_share_row_without_announce_date_is_not_available_at_period_end` —— 构造 period_end=2024-06-30 / announce_date=None / 50M 的行, 断言全程用最新维表股本兜底 (`[0.5, 0.5, 0.5]`)。
- **新增** `test_share_row_without_announce_date_column_is_not_used_for_history` —— 整表无 announce_date 列时同样不用于历史。

**证据**: `test_financial_shares.py` 全部通过; 连同 `test_custom_pct_units.py`、`test_analysis_menu_path_traversal.py`、`test_analysis_menu_url_template.py` 合计 **40 passed** (1.44s)。
**回滚方式**: 回滚 `share_capital.py` (旧行为即泄露)。
**残余风险**: **已落盘的旧 enriched 分区不会被回溯修正** → 依赖换手率的历史回测需重算后复评, 必须写入发版说明。

---

## 4. OPS-1 — 前端测试套件从未进入 CI (P1)

| 项 | 内容 |
| --- | --- |
| 问题编号 | OPS-1 |
| 维度 | ops_release |
| 严重度 / 优先级 | High / P1 |
| 状态 | CONFIRMED → **IMPLEMENTED** (CI 侧待人工确认) |
| 受影响组件 | `.github/workflows/ci.yml` |

**根因 (VERIFIED)**: ci.yml 前端 job 只跑 `pnpm install --frozen-lockfile` 与 `pnpm build` (= `tsc -b && vite build`); `package.json` 里的 `"test": "vitest run"` 未被任何 workflow 引用, 123 个前端测试文件对合并门禁而言是死代码。README.md:19 的徽章宣称 "Test: Backend + Frontend"。

**代码变更** (`.github/workflows/ci.yml`, 前端 job 内新增 2 行):
```yaml
      - name: 单元测试 (vitest)
        run: pnpm test
```
置于构建步骤之前, 阻断式。

**证据 (本地实测, 证明该门禁不会立刻变红)**: `vitest run --reporter=basic` → **Test Files 123 passed (123) / Tests 843 passed (843)**, 耗时 204.63s。
**回滚方式**: 移除该步骤。
**残余风险**: 该套件在 GitHub Actions 上的实际运行时间与并发额度未在 GitHub 侧验证 (本地为高配 Windows); 若 CI 机器较慢, 建议为该作业设置合理 timeout。

---

## 5. FE-01 — 指数页切换后展示上一只指数的日K (P1)

| 项 | 内容 |
| --- | --- |
| 问题编号 | FE-01 |
| 维度 | frontend |
| 严重度 / 优先级 | High / P1 |
| 状态 | CONFIRMED → **IMPLEMENTED / VERIFIED** |
| 受影响组件 | `frontend/src/pages/Indices.tsx` |

**根因 (VERIFIED)**: `daily` 查询的 `placeholderData: (prev) => prev` 无条件复用上一份数据; 而 `selectedInfo` 恒取新标的名称, `chartRows` 却取自旧数据的 `rows` → **标题正确、数据错误**。仓内已有正确范式 (`frontend/src/lib/kline.ts:25-28` 的"仅同 symbol 占位"守卫), 且同一文件对分钟行已用 `daily.data?.symbol === selectedSymbol` 校验, 日K 行却没有。

**代码变更** (`frontend/src/pages/Indices.tsx`):
1. `placeholderData` 改为按查询键第 1 位 (symbol) 守卫:
```tsx
    placeholderData: (prev, prevQuery) => {
      const prevKey = prevQuery?.queryKey as readonly unknown[] | undefined
      return prevKey?.[1] === selectedSymbol ? prev : undefined
    },
```
2. 双保险: `chartRows` 仅在 `daily.data?.symbol === selectedSymbol` 时渲染 (与同文件分钟行口径一致)。

**回归测试** (新增 `frontend/src/pages/Indices.symbolSwitch.test.tsx`, 1 用例): 渲染指数页 → 解析上证指数日K → 点击切换到深证成指 → 断言面板**不含**上证指数的日期, 待新指数数据返回后才展示。
**证据 (修复前 → 修复后)**:
- 回退修复后: **1 failed** — `AssertionError: expected '2026-09-09,2026-09-10' not to contain '2026-09-09'` —— **测试确实能捕获该缺陷**
- 修复后: **2 passed** (含既有 `Indices.test.tsx`)
**回滚方式**: 回滚 `Indices.tsx`。
**残余风险**: 同类问题在看板页 (FE-02) 仍存在, 因 `frontend/src/pages/Dashboard.tsx` 在审计开始时存在他人未提交改动, 本次未触碰 (见 `03-remediation-plan.md` §2.5 的注意事项)。

---

## 6. 预先存在的失败 (与本次修改无关, 已做因果对照)

| 用例 | 结果 | 因果判定 |
| --- | --- | --- |
| `tests/e2e_prod/test_tier1_health_auth_gateway.py` | 8 failed, 7 passed | **预先存在** → OPS-002 (CI 后端门禁为红) |
| `tests/test_config_backup.py::test_apply_into_fresh_env_roundtrip` | FAILED | **预先存在** —— 已做对照实验: 把 `analysis.py` 临时写回 HEAD 版本重跑, 仍是同样 2 failed / 26 passed; 写回修复版本结果相同 |
| `tests/test_config_backup.py::test_apply_conflict_default_skip_and_overwrite` | FAILED | 同上 (同一对照实验) |
| `tests/test_enriched_generation.py::test_heartbeat_renews_stale_marker` | FAILED | **预先存在** (修改前的基线运行中即失败, 位于本次未触碰的文件) |

对照实验的可复现命令见 `05-verification-report.md` §4。

## 7. 未实施的修复 (OPEN) 与原因

65 项问题中 60 项为 OPEN, 原因分三类:

1. **需要维护者决策** (OPS-002 的 A/B 选择; DATA-002 历史重算窗口) → 见 `03-remediation-plan.md` §6;
2. **需要多迭代的结构性重构** (ARC-002、ARC-004、QUAL-005、FE-004 等) → 超出本次"小步修复"的授权范围;
3. **需要外部条件** (第三方渗透测试 EXTERNAL_REQUIRED; 压测与容灾演练 NOT_EXECUTED) → 见 `01-scope-baseline-and-coverage.md` §5。

未做但本可以做的取舍说明: **FE-02 (看板日期守卫)** 与 **FE-05 (登录回跳校验)** 技术上可在本次一并修复, 但 FE-02 所在文件存在他人未提交改动 (CONTRIBUTING §1.3/§13 要求不得覆盖), FE-05 为 Low 且与核心链路无关 —— 两者均列入批次 E, 未静默跳过。
