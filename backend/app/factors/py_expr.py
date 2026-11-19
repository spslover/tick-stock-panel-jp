"""T2 表达式级 Python — 自定义因子的中间层 (P3)。

「只返回 Polars 表达式」的受限 Python: 源码先过 AST 白名单文法校验
(只有赋值与表达式语句、名字仅限 ctx/pl 与已赋值局部量、禁 import/def/
lambda/推导式/下划线开头属性), 再由纯 AST 树求值器 (_AstEvaluator)
在封闭的 _Ctx/_PlSentinel 能力白名单上求值，全程零 exec/eval/compile，
结果必须是单个 pl.Expr。

- 依赖提取: ctx.col("x") 构造期自证记录 (与 P1a 推导同目的, 但无需推导
  — 引用即登记), 进 FactorSpec.dependencies → 回测计划/P2b 缓存键同源;
- warmup: 从窗口调用常量保守推断 (rolling_* 的 window_size 与 ewm 的
  span/alpha 沿链路求和 +1)。只多不少 — 预热多算是安全方向, 少算失真;
- 安全: 纯 AST 解释器 + 封闭能力白名单, 不暴露 polars 模块对象与任何
  __builtins__/方法反射面。

三档编译层: T1 DSL (最强受限, 一切可静态分析) < 本层 T2 (表达式级自由,
依赖可追踪) < T3 自由 Python 策略 (子进程沙箱隔离)。
"""
from __future__ import annotations

import ast
import logging
import operator
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import polars as pl
from polars.expr.whenthen import ChainedThen, ChainedWhen, Then, When

from app.factors.dsl import DslError

logger = logging.getLogger(__name__)

_MAX_SOURCE_CHARS = 4000
_MAX_STATEMENTS = 30
_MAX_AST_NODES = 512
_MAX_AST_DEPTH = 24
_MAX_CONTAINER_LEN = 64
_MAX_POW_EXPONENT = 16
_MAX_POW_LARGE_BASE = 1_000_000
_MAX_SEQ_REPEAT = 64

# 文法白名单: 表达式节点 (方法链/算术/比较/三元/切片常量)
_ALLOWED_EXPR_NODES = (
    ast.Expression, ast.Expr, ast.Assign, ast.Name, ast.Attribute, ast.Call,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Constant, ast.Tuple, ast.List, ast.Subscript, ast.Slice,
)
_DISALLOWED_SYNTAX_NODES: tuple[type[ast.AST], ...] = (
    ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Import, ast.ImportFrom, ast.For, ast.AsyncFor, ast.While,
    ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Delete,
    ast.AugAssign, ast.AnnAssign, ast.Global, ast.Nonlocal,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
    ast.Yield, ast.YieldFrom, ast.Await, ast.JoinedStr, ast.FormattedValue,
    ast.NamedExpr, ast.Dict, ast.Set, ast.Starred,
    *(tuple(t for t in (getattr(ast, "TryStar", None), getattr(ast, "Match", None)) if t is not None)),
)
# 会被调度任意 Python 回调或插件/反序列化/调试的 Polars 面 — 一律拒绝。
_FORBIDDEN_ATTRS = frozenset({
    "map_batches", "map_elements", "apply", "reinterpret",
    "rolling_apply", "rolling_map", "cumulative_eval",
    "pipe", "register_plugin", "deserialize", "inspect",
})
_FORBIDDEN_PL_PREFIXES = ("read_", "scan_")
_FORBIDDEN_PL_IO_NAMES = frozenset({
    "SQLContext", "sql", "sql_expr", "Catalog", "catalog",
})
_ROOT_NAMES = frozenset({"ctx", "pl"})
_RESERVED_LITERAL_NAMES = frozenset({"True", "False", "None"})
# 窗口调用 → warmup 推断 (span=2/alpha-1 由 alpha 折算)
_WINDOW_METHODS = frozenset({
    "rolling_min", "rolling_max", "rolling_mean", "rolling_sum",
    "rolling_std", "rolling_var", "rolling_median", "rolling_quantile", "rolling_skew",
})

_PL_DTYPES: dict[str, pl.DataType] = {
    "Float64": pl.Float64,
    "Float32": pl.Float32,
    "Int64": pl.Int64,
    "Int32": pl.Int32,
    "Int16": pl.Int16,
    "Int8": pl.Int8,
    "UInt64": pl.UInt64,
    "UInt32": pl.UInt32,
    "Boolean": pl.Boolean,
    "Utf8": pl.Utf8,
    "String": pl.String,
}


def _safe_pl_lit(value: Any, dtype: Any = None, *, allow_object: bool = False) -> pl.Expr:
    if allow_object:
        raise ValueError("pl.lit 不允许 allow_object=True")
    if not isinstance(value, (int, float, bool, str, type(None))):
        raise ValueError(f"pl.lit 只允许标量字面量, 收到 {type(value).__name__}")
    if dtype is not None and not isinstance(dtype, pl.DataType) and not (
        isinstance(dtype, type) and issubclass(dtype, pl.DataType)
    ):
        raise ValueError(f"pl.lit 的 dtype 非法: {dtype!r}")
    return pl.lit(value, dtype=dtype, allow_object=False)


_PL_ALLOWED_FUNCS: dict[str, Any] = {
    "lit": _safe_pl_lit,
    "when": pl.when,
    "coalesce": pl.coalesce,
    "min_horizontal": pl.min_horizontal,
    "max_horizontal": pl.max_horizontal,
    "sum_horizontal": pl.sum_horizontal,
    "mean_horizontal": pl.mean_horizontal,
    "all_horizontal": pl.all_horizontal,
    "any_horizontal": pl.any_horizontal,
    "rolling_corr": pl.rolling_corr,
    "rolling_cov": pl.rolling_cov,
    "corr": pl.corr,
    "cov": pl.cov,
    "arctan2": pl.arctan2,
    "arctan2d": pl.arctan2d,
    "len": pl.len,
    "first": pl.first,
    "last": pl.last,
}

_EXPR_METHODS = frozenset({
    # Arithmetic / Comparison / Logic
    "abs", "add", "sub", "mul", "truediv", "floordiv", "mod", "pow", "neg",
    "eq", "ne", "lt", "le", "gt", "ge", "and_", "or_", "xor", "not_",
    "is_between", "is_in", "is_close", "eq_missing", "ne_missing",
    # Math / Transform / Cast / Clip / Round
    "sqrt", "cbrt", "log", "log10", "log1p", "exp",
    "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "degrees", "radians", "sign", "clip", "lower_bound", "upper_bound",
    "round", "round_sig_figs", "floor", "ceil", "cast", "replace", "replace_strict",
    "alias",
    # Null / NaN handling
    "fill_null", "fill_nan", "forward_fill", "backward_fill", "interpolate",
    "drop_nulls", "drop_nans", "is_null", "is_not_null", "is_nan", "is_not_nan",
    "is_finite", "is_infinite", "has_nulls", "null_count",
    # Window / Time-series / Cumulative / Aggregation
    "over", "shift", "diff", "pct_change",
    "rolling_mean", "rolling_std", "rolling_var", "rolling_min", "rolling_max",
    "rolling_sum", "rolling_median", "rolling_quantile", "rolling_skew",
    "rolling_kurtosis", "rolling_rank",
    "rolling_mean_by", "rolling_std_by", "rolling_var_by",
    "rolling_min_by", "rolling_max_by", "rolling_sum_by",
    "rolling_median_by", "rolling_quantile_by", "rolling_rank_by",
    "ewm_mean", "ewm_std", "ewm_var", "ewm_sum",
    "cum_sum", "cum_prod", "cum_min", "cum_max", "cum_count",
    "rank", "mean", "std", "var", "sum", "min", "max", "median",
    "quantile", "skew", "kurtosis", "product", "count", "len",
    "n_unique", "approx_n_unique", "first", "last", "dot",
    "sort", "sort_by", "arg_sort", "arg_max", "arg_min",
    "top_k", "bottom_k", "head", "tail", "slice", "filter", "reverse",
})

_WHEN_METHODS = frozenset({"then"})
_THEN_METHODS = frozenset({"when", "otherwise"}) | _EXPR_METHODS

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
}

_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
}

_CMP_OPS: dict[type[ast.cmpop], Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


@dataclass(frozen=True)
class CompiledPyExpr:
    ok: bool
    errors: list[DslError] = field(default_factory=list)
    expr: pl.Expr | None = None
    dependencies: frozenset[str] = frozenset()
    referenced_factors: frozenset[str] = frozenset()
    warmup_bars: int = 1
    input_windows: tuple[tuple[str, int], ...] = ()
    cross_sectional: bool = False
    formula_text: str = ""

    def frame_transform(self, frame: pl.DataFrame) -> pl.DataFrame | None:
        """与 DSL CompiledFormula.frame_transform 同形: 缺依赖返回 None (跳过),
        产出列别名 FACTOR_COLUMN (消费方按约定取列)。"""
        if self.expr is None:
            return None
        if not self.dependencies.issubset(set(frame.columns)):
            return None
        from app.factors.dsl import FACTOR_COLUMN

        return frame.with_columns(self.expr.alias(FACTOR_COLUMN))


class _PyExprDslError(Exception):
    """内部控制异常: 把求值期的特定策略拦截直接映射为稳定 DslError code。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Ctx:
    """ctx.col 构造期登记引用列 (依赖提取的单一事实来源)。"""

    def __init__(self) -> None:
        self.columns: set[str] = set()

    def col(self, name: str) -> pl.Expr:
        if not isinstance(name, str) or not name or name.startswith("__"):
            raise ValueError(f"ctx.col 的列名必须是非空字符串: {name!r}")
        self.columns.add(name)
        return pl.col(name)


class _PlSentinel:
    """不透明 pl 哨兵: 只暴露明确白名单函数与 dtype, 绝不暴露 polars 模块对象。"""


_PL = _PlSentinel()


def _window_warmup(tree: ast.Module) -> int:
    """窗口调用常量的保守求和 (rolling 的 N + ewm 的 span/alpha 折算)。"""
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else ""
        span: float | None = None
        if name in _WINDOW_METHODS:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, (int, float)):
                span = float(node.args[0].value)
            for kw in node.keywords:
                if kw.arg == "window_size" and isinstance(kw.value, ast.Constant):
                    span = float(kw.value.value)
        elif name in {"shift", "diff", "pct_change"}:
            lag = next((kw.value for kw in node.keywords if kw.arg == "n"), None)
            if lag is None:
                lag = node.args[0] if node.args else ast.Constant(1)
            if isinstance(lag, ast.Constant) and isinstance(lag.value, int) and lag.value > 0:
                span = lag.value
        elif name == "ewm_mean":
            for kw in node.keywords:
                if kw.arg == "span" and isinstance(kw.value, ast.Constant):
                    span = float(kw.value.value)
                elif kw.arg == "alpha" and isinstance(kw.value, ast.Constant):
                    alpha = float(kw.value.value)
                    span = 2.0 / alpha - 1.0 if 0 < alpha <= 1 else None
        if span is not None:
            total += max(int(span), 1)
    return total


def _ast_depth_and_count(node: ast.AST, depth: int = 1) -> tuple[int, int]:
    max_depth = depth
    count = 1
    for child in ast.iter_child_nodes(node):
        child_depth, child_count = _ast_depth_and_count(child, depth + 1)
        if child_depth > max_depth:
            max_depth = child_depth
        count += child_count
    return max_depth, count


def _validate_grammar(tree: ast.Module) -> list[DslError]:
    errors: list[DslError] = []
    if len(tree.body) == 0:
        return [DslError("E201", "表达式为空")]
    if len(tree.body) > _MAX_STATEMENTS:
        return [DslError("E202", f"语句数超限 (>{_MAX_STATEMENTS})")]
    max_depth, node_count = _ast_depth_and_count(tree)
    if node_count > _MAX_AST_NODES:
        return [DslError("E202", f"AST 节点数超限 (>{_MAX_AST_NODES})")]
    if max_depth > _MAX_AST_DEPTH:
        return [DslError("E202", f"表达式嵌套深度超限 (>{_MAX_AST_DEPTH})")]

    assigned: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if not isinstance(target, ast.Name) or not target.id.isidentifier() or target.id.startswith("_"):
                    errors.append(DslError("E203", "赋值目标必须是简单变量名 (非下划线开头)"))
                elif target.id in _ROOT_NAMES or target.id in _RESERVED_LITERAL_NAMES:
                    errors.append(DslError("E203", f"不允许覆盖内置名字: {target.id}"))
                else:
                    assigned.add(target.id)
        elif not isinstance(stmt, ast.Expr):
            errors.append(DslError("E204", f"只允许赋值与表达式语句, 不允许: {type(stmt).__name__}"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                errors.append(DslError("E205", f"禁止访问下划线属性: {node.attr}"))
            elif node.attr in _FORBIDDEN_ATTRS:
                errors.append(DslError("E206", f"禁止调用会执行任意回调的方法: {node.attr}"))
            elif isinstance(node.value, ast.Name) and node.value.id == "pl" and (
                node.attr.startswith(_FORBIDDEN_PL_PREFIXES) or node.attr in _FORBIDDEN_PL_IO_NAMES
            ):
                errors.append(DslError("E207", f"禁止 IO 类调用: pl.{node.attr}"))
        elif isinstance(node, ast.Name):
            if node.id not in assigned and node.id not in _ROOT_NAMES and node.id not in _RESERVED_LITERAL_NAMES:
                errors.append(DslError("E208", f"未定义的名字 (只允许 ctx/pl 与已赋值变量): {node.id}"))
        elif isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) > _MAX_CONTAINER_LEN:
            errors.append(DslError("E202", f"容器字面量长度超限 (>{_MAX_CONTAINER_LEN})"))
        elif isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float, bool, str, type(None))):
                errors.append(DslError("E210", f"不允许的常量类型: {type(node.value).__name__}"))
        elif isinstance(node, _DISALLOWED_SYNTAX_NODES):
            errors.append(DslError("E209", f"不允许的语法: {type(node).__name__}"))
        elif isinstance(node, ast.keyword):
            if node.arg is None or node.arg.startswith("_"):
                errors.append(DslError("E210", "禁止 **kwargs 展开或下划线关键字参数"))
        elif not isinstance(node, (*_ALLOWED_EXPR_NODES, ast.Load, ast.Store, ast.operator, ast.unaryop, ast.boolop, ast.cmpop, ast.mod, ast.expr_context, ast.keyword)):
            errors.append(DslError("E210", f"不允许的表达式节点: {type(node).__name__}"))
    return errors


def _safe_binop(op: ast.operator, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Mod) and isinstance(left, str):
        raise ValueError("禁止字符串 % 格式化")
    if isinstance(op, ast.Mult):
        if isinstance(left, (str, list, tuple)) and isinstance(right, int):
            if right < 0 or right > _MAX_SEQ_REPEAT or len(left) * max(right, 0) > _MAX_CONTAINER_LEN * 16:
                raise ValueError("序列重复乘法超限")
        elif (
            isinstance(right, (str, list, tuple))
            and isinstance(left, int)
            and (left < 0 or left > _MAX_SEQ_REPEAT or len(right) * max(left, 0) > _MAX_CONTAINER_LEN * 16)
        ):
            raise ValueError("序列重复乘法超限")
    if isinstance(op, ast.Pow) and isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, bool) or isinstance(right, bool):
            left = int(left)
            right = int(right)
        if abs(right) > _MAX_POW_EXPONENT or (right > 4 and abs(left) > _MAX_POW_LARGE_BASE):
            raise ValueError(f"指数运算超出安全边界: {left} ** {right}")
    fn = _BIN_OPS.get(type(op))
    if fn is None:
        raise ValueError(f"不允许的二元运算符: {type(op).__name__}")
    return fn(left, right)


class _AstEvaluator:
    """纯 AST 树遍历求值器: 零 exec/eval/compile 与零 __builtins__ 暴露。"""

    def __init__(self, ctx: _Ctx) -> None:
        self._ctx = ctx
        self._env: dict[str, Any] = {"ctx": ctx, "pl": _PL}

    def eval_module(self, tree: ast.Module) -> Any:
        has_result = False
        last_value: Any = None
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                value = self._eval_expr(stmt.value, depth=1)
                for target in stmt.targets:
                    assert isinstance(target, ast.Name)
                    self._env[target.id] = value
                last_value = value
                has_result = True
            elif isinstance(stmt, ast.Expr):
                last_value = self._eval_expr(stmt.value, depth=1)
                has_result = True
        if not has_result:
            raise _PyExprDslError("E211", "缺少结果表达式 (末尾裸表达式或赋值)")
        return last_value

    def _resolve_attr(self, target: Any, attr: str) -> Any:
        if attr.startswith("_"):
            raise _PyExprDslError("E205", f"禁止访问下划线属性: {attr}")
        if attr in _FORBIDDEN_ATTRS:
            raise _PyExprDslError("E206", f"禁止调用会执行任意回调的方法: {attr}")

        if target is self._ctx:
            if attr == "col":
                return self._ctx.col
            raise ValueError(f"ctx 不支持属性: {attr}")

        if target is _PL:
            if attr == "col":
                raise _PyExprDslError("E214", "禁止直接调用 pl.col，请至少通过 ctx.col 引用输入列")
            if attr.startswith(_FORBIDDEN_PL_PREFIXES) or attr in _FORBIDDEN_PL_IO_NAMES:
                raise _PyExprDslError("E207", f"禁止 IO 类调用: pl.{attr}")
            if attr in _PL_ALLOWED_FUNCS:
                return _PL_ALLOWED_FUNCS[attr]
            if attr in _PL_DTYPES:
                return _PL_DTYPES[attr]
            raise ValueError(f"pl 不允许访问: {attr}")

        if isinstance(target, (When, ChainedWhen)):
            if attr in _WHEN_METHODS:
                return getattr(target, attr)
            raise ValueError(f"When 表达式不支持方法: {attr}")

        if isinstance(target, (Then, ChainedThen)):
            if attr in _THEN_METHODS:
                return getattr(target, attr)
            raise ValueError(f"Then 表达式不支持方法: {attr}")

        if isinstance(target, pl.Expr):
            if attr in _EXPR_METHODS:
                return getattr(target, attr)
            raise ValueError(f"pl.Expr 不允许方法/属性: {attr}")

        raise ValueError(f"类型 {type(target).__name__} 不允许属性访问: {attr}")

    def _eval_expr(self, node: ast.AST, depth: int) -> Any:
        if depth > _MAX_AST_DEPTH:
            raise _PyExprDslError("E202", f"表达式求值深度超限 (>{_MAX_AST_DEPTH})")

        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float, bool, str, type(None))):
                raise ValueError(f"不允许的常量类型: {type(node.value).__name__}")
            return node.value

        if isinstance(node, ast.Name):
            if node.id == "True":
                return True
            if node.id == "False":
                return False
            if node.id == "None":
                return None
            if node.id in self._env:
                return self._env[node.id]
            raise _PyExprDslError("E208", f"未定义的名字 (只允许 ctx/pl 与已赋值变量): {node.id}")

        if isinstance(node, ast.Tuple):
            if len(node.elts) > _MAX_CONTAINER_LEN:
                raise _PyExprDslError("E202", f"元组长度超限 (>{_MAX_CONTAINER_LEN})")
            return tuple(self._eval_expr(elt, depth + 1) for elt in node.elts)

        if isinstance(node, ast.List):
            if len(node.elts) > _MAX_CONTAINER_LEN:
                raise _PyExprDslError("E202", f"列表长度超限 (>{_MAX_CONTAINER_LEN})")
            return [self._eval_expr(elt, depth + 1) for elt in node.elts]

        if isinstance(node, ast.Attribute):
            target = self._eval_expr(node.value, depth + 1)
            return self._resolve_attr(target, node.attr)

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                raise ValueError("只允许调用 ctx/pl/表达式上的白名单方法")
            target = self._eval_expr(node.func.value, depth + 1)
            callable_fn = self._resolve_attr(target, node.func.attr)
            if not callable(callable_fn):
                raise TypeError(f"{node.func.attr} 不可调用")
            args: list[Any] = []
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    raise ValueError("禁止 *args 展开")
                args.append(self._eval_expr(arg, depth + 1))
            kwargs: dict[str, Any] = {}
            for kw in node.keywords:
                if kw.arg is None or kw.arg.startswith("_"):
                    raise ValueError("禁止 **kwargs 展开或下划线关键字参数")
                kwargs[kw.arg] = self._eval_expr(kw.value, depth + 1)
            return callable_fn(*args, **kwargs)

        if isinstance(node, ast.BinOp):
            left = self._eval_expr(node.left, depth + 1)
            right = self._eval_expr(node.right, depth + 1)
            return _safe_binop(node.op, left, right)

        if isinstance(node, ast.UnaryOp):
            operand = self._eval_expr(node.operand, depth + 1)
            fn = _UNARY_OPS.get(type(node.op))
            if fn is None:
                raise ValueError(f"不允许的一元运算符: {type(node.op).__name__}")
            return fn(operand)

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                val = self._eval_expr(node.values[0], depth + 1)
                for item in node.values[1:]:
                    if not val:
                        return val
                    val = self._eval_expr(item, depth + 1)
                return val
            if isinstance(node.op, ast.Or):
                val = self._eval_expr(node.values[0], depth + 1)
                for item in node.values[1:]:
                    if val:
                        return val
                    val = self._eval_expr(item, depth + 1)
                return val
            raise ValueError(f"不允许的布尔运算符: {type(node.op).__name__}")

        if isinstance(node, ast.Compare):
            left = self._eval_expr(node.left, depth + 1)
            if len(node.ops) == 1:
                cmp_fn = _CMP_OPS.get(type(node.ops[0]))
                if cmp_fn is None:
                    raise ValueError(f"不允许的比较运算符: {type(node.ops[0]).__name__}")
                right = self._eval_expr(node.comparators[0], depth + 1)
                return cmp_fn(left, right)
            cur = left
            for op, comp_node in zip(node.ops, node.comparators, strict=True):
                cmp_fn = _CMP_OPS.get(type(op))
                if cmp_fn is None:
                    raise ValueError(f"不允许的比较运算符: {type(op).__name__}")
                nxt = self._eval_expr(comp_node, depth + 1)
                if not cmp_fn(cur, nxt):
                    return False
                cur = nxt
            return True

        if isinstance(node, ast.IfExp):
            cond = self._eval_expr(node.test, depth + 1)
            branch = node.body if cond else node.orelse
            return self._eval_expr(branch, depth + 1)

        if isinstance(node, ast.Subscript):
            container = self._eval_expr(node.value, depth + 1)
            if not isinstance(container, (tuple, list, str)):
                raise ValueError(f"禁止对 {type(container).__name__} 进行下标访问")
            if isinstance(node.slice, ast.Slice):
                lower = self._eval_expr(node.slice.lower, depth + 1) if node.slice.lower is not None else None
                upper = self._eval_expr(node.slice.upper, depth + 1) if node.slice.upper is not None else None
                step = self._eval_expr(node.slice.step, depth + 1) if node.slice.step is not None else None
                for bound in (lower, upper, step):
                    if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
                        raise ValueError("切片边界必须是整数或 None")
                return container[slice(lower, upper, step)]
            idx = self._eval_expr(node.slice, depth + 1)
            if isinstance(idx, bool) or not isinstance(idx, int):
                raise ValueError("下标必须是整数")
            return container[idx]

        raise ValueError(f"不允许的表达式节点: {type(node).__name__}")


def compile_python_expression(source: str) -> CompiledPyExpr:
    """编译 T2 表达式; 永不抛异常, 失败以 errors 表达 (fail-closed)。"""
    if not isinstance(source, str) or not source.strip():
        return CompiledPyExpr(ok=False, errors=[DslError("E201", "表达式为空")], formula_text=source or "")
    if len(source) > _MAX_SOURCE_CHARS:
        return CompiledPyExpr(ok=False, errors=[DslError("E202", f"源码超长 (>{_MAX_SOURCE_CHARS} 字符)")], formula_text=source)
    try:
        tree = ast.parse(textwrap.dedent(source), mode="exec")
    except SyntaxError as e:
        return CompiledPyExpr(ok=False, errors=[DslError("E201", f"语法错误: {e.msg}")], formula_text=source)

    errors = _validate_grammar(tree)
    if errors:
        return CompiledPyExpr(ok=False, errors=errors, formula_text=source)

    ctx = _Ctx()
    evaluator = _AstEvaluator(ctx)
    try:
        result = evaluator.eval_module(tree)
    except _PyExprDslError as e:
        return CompiledPyExpr(ok=False, errors=[DslError(e.code, e.message)], formula_text=source)
    except Exception as e:  # noqa: BLE001 — 用户表达式求值失败统一转为编译错误
        return CompiledPyExpr(ok=False, errors=[DslError("E212", f"求值失败: {type(e).__name__}: {e}")], formula_text=source)

    if not isinstance(result, pl.Expr):
        return CompiledPyExpr(ok=False, errors=[DslError("E213", "结果必须是单个 Polars 表达式 (pl.Expr)")], formula_text=source)
    if not ctx.columns:
        return CompiledPyExpr(ok=False, errors=[DslError("E214", "至少通过 ctx.col 引用一个输入列")], formula_text=source)

    # 引用名里命中注册表因子 id 的, 单列为 referenced_factors (成员须先物化)
    from app.factors.registry import factor_warmup_bars, get_factor

    referenced = frozenset(c for c in ctx.columns if get_factor(c) is not None)
    bars = _window_warmup(tree)
    return CompiledPyExpr(
        ok=True,
        expr=result,
        dependencies=frozenset(ctx.columns),
        referenced_factors=referenced,
        warmup_bars=factor_warmup_bars(ctx.columns) + bars,
        input_windows=tuple((name, bars) for name in sorted(ctx.columns)),
        formula_text=source,
    )


@lru_cache(maxsize=256)
def _compile_cached(source: str, revision: int) -> CompiledPyExpr:
    return compile_python_expression(source)


def compile_python_expression_cached(source: str) -> CompiledPyExpr:
    from app.factors.registry import registry_cache_revision

    revision = registry_cache_revision()
    return compile_python_expression(source) if revision is None else _compile_cached(source, revision)
