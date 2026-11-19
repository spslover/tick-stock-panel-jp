"""Hardened out-of-process execution sandbox for untrusted strategies & policies.

Design invariants (C-01 + H-03):
- Zero `pickle` / `marshal` / `np.load(..., allow_pickle=True)` on child -> parent IPC.
  Uses length-framed JSON headers + Arrow IPC DataFrames + NumPy `.npy` (allow_pickle=False)
  + AST-validated Polars `Expr` JSON (`format="json"`).
- Child launches with `python -I` (isolated mode) in an empty `TemporaryDirectory` cwd,
  with stripped OS-only environment variables (`DATA_DIR` and `TICKFLOW_ENV_FILE`
  explicitly redirected to empty/missing temp paths so host `secrets.json` and `.env`
  are unreachable).
- OS resource limits:
  - Windows: Win32 Job Object (`KILL_ON_JOB_CLOSE | PROCESS_MEMORY | ACTIVE_PROCESS=1`)
    plus wall-clock timer + `psutil` tree-kill fallback.
  - POSIX: `start_new_session=True`, `resource.setrlimit` (`RLIMIT_CORE=0`,
    `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_DATA`, `RLIMIT_NPROC=0` best-effort)
    plus wall-clock timer (`os.killpg(..., SIGKILL)`) + optional `psutil` RSS check.
- Child loads untrusted code under UUID-namespaced module names
  (`_tsp_strategy_<uuid>` / `_tsp_policy_<uuid>`) with restricted `__builtins__`
  (no `open`, `exec`, `eval`, `compile`, `getattr`, `setattr`, `delattr`, `globals`,
  `locals`, `vars`, `dir`, `breakpoint`, `input`, `memoryview`, `exit`, `quit`,
  `importlib`) and gated `_safe_import`, popping temporary modules in `finally`.
- `app.strategy.market_data` in child is replaced with a read-only RPC stub calling
  back to the parent over the framed pipe so `data_dir` is never exposed to child.
"""
from __future__ import annotations

import argparse
import atexit
import builtins as _py_builtins
import ctypes
import ctypes.wintypes as _wt
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import logging
import os
import signal
import site
import struct
import subprocess
import sys
import tempfile
import threading
import types
import uuid
import weakref
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

if __name__ == "__main__" and "--worker" in sys.argv:
    for _idx, _arg in enumerate(sys.argv):
        if _arg == "--site-dir" and _idx + 1 < len(sys.argv):
            _sdir = sys.argv[_idx + 1]
            if _sdir and Path(_sdir).is_dir():
                site.addsitedir(_sdir)

import numpy as np
import polars as pl

try:
    import psutil  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_WORKER_SCRIPT = Path(__file__).resolve()

_MAX_HEADER_BYTES = 16 * 1024 * 1024
_MAX_ATTACHMENT_BYTES = 512 * 1024 * 1024
_DEFAULT_MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024  # 1.5 GiB (safe for Polars/NumPy)
_POLICY_MEMORY_LIMIT_BYTES = 768 * 1024 * 1024    # 768 MiB
_DEFAULT_INSPECT_TIMEOUT_S = 15.0
_DEFAULT_EXEC_TIMEOUT_S = 60.0
_DEFAULT_POLICY_TIMEOUT_S = 5.0

_FORBIDDEN_POLARS_EXPR_KEYS = frozenset({
    "OpaquePython",
    "FfiPlugin",
    "PythonFunction",
    "AnonymousFunction",
    "CloudPickle",
    "PythonScan",
    "External",
    "Scan",
})

_STRATEGY_ALLOWED_EXACT_MODULES = frozenset({
    "polars",
    "numpy",
    "app.backtest.matrix",
    "app.strategy.research.factor_rank_research",
    "app.strategy.builtin.factor_rank_research",
    "app.strategy.market_data",
    "datetime",
    "__future__",
})

_DEP_ALLOWED_EXTRA_MODULES = frozenset({
    "collections",
    "collections.abc",
    "types",
    "typing",
})

_POLICY_ALLOWED_EXACT_MODULES = frozenset({
    "polars",
    "numpy",
    "app.backtest.position_sizing",
    "datetime",
    "math",
    "__future__",
})

_ALLOWED_IMPORT_SUBMODULE_ROOTS = frozenset({"polars", "numpy", "datetime", "math"})


class SandboxError(RuntimeError):
    """Base failure for sandbox execution."""


class SandboxTimeoutError(SandboxError):
    """Sandboxed execution exceeded wall-clock timeout."""


class SandboxMemoryError(SandboxError):
    """Sandboxed execution exceeded OS memory limit."""


class SandboxSecurityError(SandboxError, ValueError):
    """Sandboxed code violated safety or deserialization policy."""


# ================================================================
# Framed Binary IPC Protocol (Zero Pickle)
# ================================================================

def _read_exact(stream: Any, length: int) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        chunk = stream.read(length - len(buf))
        if not chunk:
            raise EOFError("sandbox pipe closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


def _write_frame(
    stream: Any,
    header: dict[str, Any],
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> None:
    atts = attachments or []
    meta_list = [
        {"name": name, "kind": kind, "length": len(data)}
        for name, kind, data in atts
    ]
    payload = {**header, "attachments": meta_list}
    header_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(header_bytes) > _MAX_HEADER_BYTES:
        raise SandboxError("IPC frame header exceeds maximum size")
    stream.write(struct.pack(">I", len(header_bytes)))
    stream.write(header_bytes)
    for _, _, data in atts:
        if data:
            stream.write(data)
    stream.flush()


def _read_frame(stream: Any) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    raw_len = _read_exact(stream, 4)
    (header_len,) = struct.unpack(">I", raw_len)
    if header_len == 0 or header_len > _MAX_HEADER_BYTES:
        raise SandboxSecurityError(f"invalid IPC frame header length: {header_len}")
    header_bytes = _read_exact(stream, header_len)
    header = json.loads(header_bytes.decode("utf-8"))
    if not isinstance(header, dict):
        raise SandboxSecurityError("IPC frame header must be a JSON object")
    attachments_meta = header.get("attachments") or []
    if not isinstance(attachments_meta, list):
        raise SandboxSecurityError("IPC attachments field must be a list")
    total_bytes = 0
    attachments: dict[str, tuple[str, bytes]] = {}
    for item in attachments_meta:
        if not isinstance(item, dict):
            raise SandboxSecurityError("IPC attachment entry must be an object")
        name = str(item.get("name", ""))
        kind = str(item.get("kind", ""))
        length = int(item.get("length", 0))
        if length < 0 or total_bytes + length > _MAX_ATTACHMENT_BYTES:
            raise SandboxSecurityError("IPC attachment exceeds maximum size")
        total_bytes += length
        data = _read_exact(stream, length) if length else b""
        attachments[name] = (kind, data)
    return header, attachments


def _encode_df(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_ipc(buf)
    return buf.getvalue()


def _decode_df(data: bytes) -> pl.DataFrame:
    return pl.read_ipc(io.BytesIO(data))


def _encode_ndarray(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    contiguous = np.ascontiguousarray(arr)
    np.lib.format.write_array(buf, contiguous, allow_pickle=False)
    return buf.getvalue()


def _decode_ndarray(data: bytes) -> np.ndarray:
    arr = np.lib.format.read_array(io.BytesIO(data), allow_pickle=False)
    arr.flags.writeable = False
    return arr


def _validate_polars_expr_json(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _FORBIDDEN_POLARS_EXPR_KEYS:
                raise SandboxSecurityError(
                    f"disallowed Polars expression node in sandbox output: {key}"
                )
            _validate_polars_expr_json(value)
    elif isinstance(node, list):
        for item in node:
            _validate_polars_expr_json(item)


def _decode_polars_expr(is_none: bool, raw_json: str | None) -> pl.Expr | None:
    if is_none:
        return None
    if not isinstance(raw_json, str) or not raw_json:
        raise SandboxSecurityError("missing Polars expression JSON from sandbox")
    parsed = json.loads(raw_json)
    _validate_polars_expr_json(parsed)
    return pl.Expr.deserialize(io.StringIO(raw_json), format="json")


# ================================================================
# OS Process Isolation (Env, Job Objects, rlimits, Tree-Kill)
# ================================================================

def _build_stripped_env(tmp_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    no_data_dir = tmp_dir / "_isolated_data"
    no_data_dir.mkdir(parents=True, exist_ok=True)
    no_env_file = tmp_dir / "_isolated_env_missing"

    env: dict[str, str] = {}
    if os.name == "nt":
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH"):
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
    else:
        for key in ("PATH", "LANG", "LC_ALL"):
            val = os.environ.get(key)
            if val is not None:
                env[key] = val

    env.update({
        "DATA_DIR": str(no_data_dir),
        "TICKFLOW_ENV_FILE": str(no_env_file),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
        "HOME": str(tmp_dir),
        "USERPROFILE": str(tmp_dir),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "POLARS_MAX_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "RAYON_NUM_THREADS": "1",
    })
    if extra:
        env.update(extra)
    return env


def _worker_command() -> tuple[list[str], dict[str, str], int]:
    """Return (argv, extra_env, active_process_limit)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sandbox-worker"], {"TSP_SANDBOX_WORKER": "1"}, 1

    base_exe = getattr(sys, "_base_executable", None) or sys.executable
    if not Path(base_exe).exists():
        base_exe = sys.executable

    uses_trampoline = (
        os.name == "nt"
        and Path(base_exe).resolve() == Path(sys.executable).resolve()
        and sys.prefix != sys.base_prefix
    )
    active_limit = 2 if uses_trampoline else 1

    cmd = [
        str(base_exe),
        "-I",
        str(_WORKER_SCRIPT),
        "--app-root",
        str(_BACKEND_ROOT),
        "--worker",
    ]
    seen_sites: set[str] = set()
    for entry in sys.path:
        low = entry.lower()
        if ("site-packages" in low or "dist-packages" in low) and entry not in seen_sites:
            seen_sites.add(entry)
            cmd.extend(["--site-dir", entry])
    return cmd, {}, active_limit


if os.name == "nt":
    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", _wt.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", _wt.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", _wt.DWORD),
            ("SchedulingClass", _wt.DWORD),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _Win32JobGuard:
    """Best-effort Win32 Job Object enforcing kill-on-close, memory cap, and process limit."""

    def __init__(self, memory_limit_bytes: int, active_process_limit: int) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JobObjectExtendedLimitInformation()
        # KILL_ON_JOB_CLOSE (0x2000) | PROCESS_MEMORY (0x100) | ACTIVE_PROCESS (0x8)
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x8
        info.BasicLimitInformation.ActiveProcessLimit = max(1, int(active_process_limit))
        info.ProcessMemoryLimit = max(64 * 1024 * 1024, int(memory_limit_bytes))
        ok = _kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            _kernel32.CloseHandle(handle)
            return
        self._handle = int(handle)

    def assign(self, proc: subprocess.Popen[bytes]) -> None:
        if self._handle is not None and os.name == "nt":
            _kernel32.AssignProcessToJobObject(self._handle, int(proc._handle))

    def terminate(self) -> None:
        if self._handle is not None and os.name == "nt":
            _kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None and os.name == "nt":
            _kernel32.CloseHandle(self._handle)
            self._handle = None


def _kill_process_tree(proc: subprocess.Popen[bytes], job: _Win32JobGuard | None) -> None:
    if job is not None:
        with suppress(Exception):
            job.terminate()
    if proc.poll() is None and psutil is not None:
        with suppress(Exception):
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                with suppress(Exception):
                    child.kill()
            parent.kill()
    if proc.poll() is None and os.name != "nt":
        with suppress(Exception):
            os.killpg(proc.pid, signal.SIGKILL)
    if proc.poll() is None:
        with suppress(Exception):
            proc.kill()


def _apply_posix_limits(cpu_limit_s: int, memory_limit_bytes: int) -> None:
    if os.name == "nt":
        return
    try:
        import resource
    except ImportError:
        return

    def _try_limit(res: int, soft: int, hard: int) -> None:
        with suppress(OSError, ValueError):
            resource.setrlimit(res, (soft, hard))

    if hasattr(resource, "RLIMIT_CORE"):
        _try_limit(resource.RLIMIT_CORE, 0, 0)
    if hasattr(resource, "RLIMIT_CPU"):
        limit = max(1, int(cpu_limit_s))
        _try_limit(resource.RLIMIT_CPU, limit, limit)
    if hasattr(resource, "RLIMIT_NOFILE"):
        _try_limit(resource.RLIMIT_NOFILE, 256, 256)
    if hasattr(resource, "RLIMIT_DATA"):
        mem = max(128 * 1024 * 1024, int(memory_limit_bytes))
        _try_limit(resource.RLIMIT_DATA, mem, mem)
    if hasattr(resource, "RLIMIT_NPROC"):
        _try_limit(resource.RLIMIT_NPROC, 0, 0)


# ================================================================
# Host Market-Data RPC Bridge
# ================================================================

def _handle_market_data_rpc(header: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]]:
    from app.strategy import market_data

    method = str(header.get("method", ""))
    args = header.get("args") or {}
    if not isinstance(args, dict):
        return {"type": "rpc_reply", "ok": False, "error": "invalid rpc args"}, []

    try:
        if method in {"get_index_daily", "get_etf_daily", "get_daily", "get_ext_timeseries"}:
            fn = getattr(market_data, method)
            df = fn(
                args.get("symbol") if method != "get_ext_timeseries" else args.get("config_id"),
                start=args.get("start"),
                end=args.get("end"),
                columns=args.get("columns"),
            )
            if not isinstance(df, pl.DataFrame):
                df = pl.DataFrame()
            return {"type": "rpc_reply", "ok": True, "kind": "df"}, [("df", "arrow", _encode_df(df))]
        if method == "list_index_symbols":
            rows = market_data.list_index_symbols()
            return {"type": "rpc_reply", "ok": True, "kind": "json", "value": rows}, []
        return {"type": "rpc_reply", "ok": False, "error": f"unsupported market_data method: {method}"}, []
    except Exception as exc:  # noqa: BLE001
        return {"type": "rpc_reply", "ok": False, "error": f"{type(exc).__name__}: {exc}"}, []


# ================================================================
# Host-Side Sandbox Session & One-Shot Callers
# ================================================================

class SandboxSession:
    """Scoped persistent hardened subprocess for repeated sandbox calls."""

    def __init__(
        self,
        *,
        memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
        cpu_limit_s: int = 120,
    ) -> None:
        self._memory_limit_bytes = int(memory_limit_bytes)
        self._cpu_limit_s = int(cpu_limit_s)
        self._lock = threading.RLock()
        self._closed = False
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._job: _Win32JobGuard | None = None
        self._finalizer = weakref.finalize(
            self,
            _finalize_session,
            lambda: self._proc,
            lambda: self._job,
            lambda: self._tmp_dir,
        )

    def _ensure_started(self) -> subprocess.Popen[bytes]:
        if self._closed:
            raise SandboxError("sandbox session is closed")
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        self._cleanup_process()
        tmp = tempfile.TemporaryDirectory(prefix="tsp_sandbox_")
        self._tmp_dir = tmp
        cmd, extra_env, active_limit = _worker_command()
        cmd = [*cmd, "--cpu-limit", str(self._cpu_limit_s), "--memory-limit", str(self._memory_limit_bytes)]
        env = _build_stripped_env(Path(tmp.name), extra=extra_env)

        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": tmp.name,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True

        job = _Win32JobGuard(self._memory_limit_bytes, active_limit)
        proc = subprocess.Popen(cmd, **kwargs)
        job.assign(proc)
        self._job = job
        self._proc = proc
        return proc

    def request(
        self,
        header: dict[str, Any],
        attachments: list[tuple[str, str, bytes]] | None = None,
        *,
        timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
    ) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
        with self._lock:
            proc = self._ensure_started()
            assert proc.stdin is not None and proc.stdout is not None
            timed_out = threading.Event()
            oom_killed = threading.Event()

            def _on_timeout() -> None:
                timed_out.set()
                _kill_process_tree(proc, self._job)

            timer = threading.Timer(float(timeout_s), _on_timeout)
            timer.daemon = True
            timer.start()
            rss_stop = threading.Event()
            rss_thread: threading.Thread | None = None
            if os.name != "nt" and psutil is not None:
                def _watch_rss() -> None:
                    while not rss_stop.wait(0.1):
                        if proc.poll() is not None:
                            return
                        try:
                            rss = psutil.Process(proc.pid).memory_info().rss
                            if rss > self._memory_limit_bytes:
                                oom_killed.set()
                                _kill_process_tree(proc, self._job)
                                return
                        except Exception:  # noqa: BLE001
                            return

                rss_thread = threading.Thread(target=_watch_rss, daemon=True)
                rss_thread.start()

            try:
                _write_frame(proc.stdin, header, attachments)
                while True:
                    resp_header, resp_atts = _read_frame(proc.stdout)
                    msg_type = resp_header.get("type")
                    if msg_type == "rpc_market_data":
                        reply_header, reply_atts = _handle_market_data_rpc(resp_header)
                        _write_frame(proc.stdin, reply_header, reply_atts)
                        continue
                    if msg_type == "result":
                        return resp_header, resp_atts
                    raise SandboxSecurityError(f"unexpected sandbox frame type: {msg_type!r}")
            except (EOFError, BrokenPipeError, OSError) as exc:
                stderr_tail = ""
                if proc.stderr is not None:
                    with suppress(Exception):
                        proc.wait(timeout=0.5)
                    with suppress(Exception):
                        stderr_tail = proc.stderr.read().decode("utf-8", errors="replace")[-600:]
                self._cleanup_process()
                if timed_out.is_set():
                    raise SandboxTimeoutError(
                        f"sandbox execution timed out after {timeout_s:.1f}s"
                    ) from exc
                if oom_killed.is_set() or proc.returncode in (-9, 137, -1073741801, -1073741571):
                    raise SandboxMemoryError(
                        f"sandbox process terminated due to memory/resource limit ({stderr_tail.strip()})"
                    ) from exc
                if "MemoryError" in stderr_tail:
                    raise SandboxMemoryError(f"sandbox MemoryError: {stderr_tail.strip()}") from exc
                raise SandboxError(
                    f"sandbox process exited unexpectedly (rc={proc.returncode}): {stderr_tail.strip()}"
                ) from exc
            finally:
                timer.cancel()
                rss_stop.set()
                if rss_thread is not None:
                    rss_thread.join(timeout=0.2)

    def _cleanup_process(self) -> None:
        proc = self._proc
        job = self._job
        tmp = self._tmp_dir
        self._proc = None
        self._job = None
        self._tmp_dir = None
        if proc is not None:
            if proc.stdin is not None:
                with suppress(Exception):
                    proc.stdin.close()
            if proc.poll() is None:
                _kill_process_tree(proc, job)
                with suppress(Exception):
                    proc.wait(timeout=1.0)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    with suppress(Exception):
                        stream.close()
        if job is not None:
            job.close()
        if tmp is not None:
            with suppress(Exception):
                tmp.cleanup()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cleanup_process()

    def __enter__(self) -> SandboxSession:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _finalize_session(get_proc: Callable, get_job: Callable, get_tmp: Callable) -> None:
    proc = get_proc()
    job = get_job()
    tmp = get_tmp()
    if proc is not None and proc.poll() is None:
        _kill_process_tree(proc, job)
    if job is not None:
        job.close()
    if tmp is not None:
        with suppress(Exception):
            tmp.cleanup()


_ACTIVE_SESSIONS: weakref.WeakSet[SandboxSession] = weakref.WeakSet()


@atexit.register
def _cleanup_all_sessions() -> None:
    for session in list(_ACTIVE_SESSIONS):
        with suppress(Exception):
            session.close()


def create_session(
    *,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
    cpu_limit_s: int = 120,
) -> SandboxSession:
    session = SandboxSession(
        memory_limit_bytes=memory_limit_bytes,
        cpu_limit_s=cpu_limit_s,
    )
    _ACTIVE_SESSIONS.add(session)
    return session


def _raise_worker_error(header: dict[str, Any]) -> None:
    err_type = str(header.get("error_type", "RuntimeError"))
    err_msg = str(header.get("error", "sandbox error"))
    if err_type == "ValueError":
        raise ValueError(err_msg)
    if err_type == "TypeError":
        raise TypeError(err_msg)
    if err_type == "SyntaxError":
        raise ValueError(f"策略语法错误: {err_msg}")
    if err_type == "MemoryError":
        raise SandboxMemoryError(err_msg)
    if err_type == "SandboxSecurityError":
        raise SandboxSecurityError(err_msg)
    raise SandboxError(f"{err_type}: {err_msg}")


def inspect_strategy(
    path: Path,
    *,
    timeout_s: float = _DEFAULT_INSPECT_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> dict[str, Any]:
    """Validate and inspect an untrusted strategy file in a fresh sandbox subprocess."""
    with create_session(memory_limit_bytes=memory_limit_bytes, cpu_limit_s=int(max(5, timeout_s))) as sess:
        resp, _ = sess.request(
            {"op": "inspect_strategy", "path": str(path)},
            timeout_s=timeout_s,
        )
        if not resp.get("ok"):
            _raise_worker_error(resp)
        return dict(resp["data"])


def dry_validate_strategy_code(
    code: str,
    *,
    timeout_s: float = _DEFAULT_INSPECT_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> None:
    """Dry-load AI/custom strategy code inside a fresh sandbox subprocess."""
    with create_session(memory_limit_bytes=memory_limit_bytes, cpu_limit_s=int(max(5, timeout_s))) as sess:
        resp, _ = sess.request(
            {"op": "dry_validate_strategy", "code": code},
            timeout_s=timeout_s,
        )
        if not resp.get("ok"):
            _raise_worker_error(resp)


def run_filter_expr(
    path: Path,
    df: pl.DataFrame,
    params: dict[str, Any],
    *,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> pl.Expr | None:
    atts = [("df", "arrow", _encode_df(df))]
    req = {"op": "run_filter_expr", "path": str(path), "params": _json_safe_dict(params)}
    if session is not None:
        resp, _ = session.request(req, atts, timeout_s=timeout_s)
    else:
        with create_session(memory_limit_bytes=memory_limit_bytes) as sess:
            resp, _ = sess.request(req, atts, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return _decode_polars_expr(bool(resp.get("is_none")), resp.get("expr_json"))


def run_filter_history(
    path: Path,
    df: pl.DataFrame,
    params: dict[str, Any],
    *,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> pl.DataFrame:
    atts = [("df", "arrow", _encode_df(df))]
    req = {"op": "run_filter_history", "path": str(path), "params": _json_safe_dict(params)}
    if session is not None:
        resp, resp_atts = session.request(req, atts, timeout_s=timeout_s)
    else:
        with create_session(memory_limit_bytes=memory_limit_bytes) as sess:
            resp, resp_atts = sess.request(req, atts, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return _decode_df(resp_atts["df"][1])


def run_filter_minute_history(
    path: Path,
    history: pl.DataFrame,
    params: dict[str, Any],
    *,
    daily: pl.DataFrame | None = None,
    has_daily: bool = False,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> pl.DataFrame:
    atts = [("history", "arrow", _encode_df(history))]
    if has_daily and daily is not None:
        atts.append(("daily", "arrow", _encode_df(daily)))
    req = {
        "op": "run_filter_minute_history",
        "path": str(path),
        "params": _json_safe_dict(params),
        "has_daily": has_daily,
        "daily_is_none": daily is None,
    }
    if session is not None:
        resp, resp_atts = session.request(req, atts, timeout_s=timeout_s)
    else:
        with create_session(memory_limit_bytes=memory_limit_bytes) as sess:
            resp, resp_atts = sess.request(req, atts, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return _decode_df(resp_atts["df"][1])


def run_matrix_required_warmup_bars(
    path: Path,
    params: dict[str, Any],
    *,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_INSPECT_TIMEOUT_S,
) -> int:
    req = {
        "op": "matrix_required_warmup_bars",
        "path": str(path),
        "params": _json_safe_dict(params),
    }
    if session is not None:
        resp, _ = session.request(req, timeout_s=timeout_s)
    else:
        with create_session() as sess:
            resp, _ = sess.request(req, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return int(resp["value"])


def run_matrix_required_fields_for_params(
    path: Path,
    params: dict[str, Any],
    *,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_INSPECT_TIMEOUT_S,
) -> frozenset[str]:
    req = {
        "op": "matrix_required_fields_for_params",
        "path": str(path),
        "params": _json_safe_dict(params),
    }
    if session is not None:
        resp, _ = session.request(req, timeout_s=timeout_s)
    else:
        with create_session() as sess:
            resp, _ = sess.request(req, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return frozenset(str(x) for x in resp["value"])


def run_matrix_compute_signals(
    path: Path,
    market: Any,
    params: dict[str, Any],
    *,
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
    memory_limit_bytes: int = _DEFAULT_MEMORY_LIMIT_BYTES,
) -> Any:
    from app.backtest.matrix import SignalMatrix, validate_signal_matrix

    core_names = (
        "timestamps",
        "session_ids",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "tradable",
        "limit_up_locked",
        "limit_down_locked",
    )
    atts: list[tuple[str, str, bytes]] = [
        (name, "npy", _encode_ndarray(getattr(market, name)))
        for name in core_names
    ]
    field_names = list(market.fields.keys())
    for fname in field_names:
        atts.append((f"field:{fname}", "npy", _encode_ndarray(market.fields[fname])))

    req = {
        "op": "matrix_compute_signals",
        "path": str(path),
        "params": _json_safe_dict(params),
        "market_meta": {
            "timestamp_labels": list(market.timestamp_labels),
            "symbols": list(market.symbols),
            "names": list(market.names),
            "field_names": field_names,
            "vector_fields": list(getattr(market, "vector_fields", ())),
        },
    }
    if session is not None:
        resp, resp_atts = session.request(req, atts, timeout_s=timeout_s)
    else:
        with create_session(memory_limit_bytes=memory_limit_bytes) as sess:
            resp, resp_atts = sess.request(req, atts, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)

    sig = SignalMatrix(
        entry=_decode_ndarray(resp_atts["entry"][1]),
        exit=_decode_ndarray(resp_atts["exit"][1]),
        score=_decode_ndarray(resp_atts["score"][1]),
        entry_signal_code=_decode_ndarray(resp_atts["entry_signal_code"][1]),
        exit_signal_code=_decode_ndarray(resp_atts["exit_signal_code"][1]),
        entry_signal_ids=tuple(str(x) for x in resp.get("entry_signal_ids", ())),
        exit_signal_ids=tuple(str(x) for x in resp.get("exit_signal_ids", ())),
    )
    validate_signal_matrix(sig, market.shape)
    return sig


def smoke_position_policy(
    code: str,
    *,
    source_hint: str = "<validate>",
    timeout_s: float = _DEFAULT_POLICY_TIMEOUT_S,
    memory_limit_bytes: int = _POLICY_MEMORY_LIMIT_BYTES,
) -> dict[str, Any]:
    """Validate and smoke-test a position policy strictly inside the sandbox subprocess."""
    try:
        with create_session(
            memory_limit_bytes=memory_limit_bytes,
            cpu_limit_s=int(max(2, timeout_s)),
        ) as sess:
            resp, _ = sess.request(
                {"op": "smoke_position_policy", "code": code, "source_hint": source_hint},
                timeout_s=timeout_s,
            )
    except SandboxError as exc:
        return {"ok": False, "issues": [f"加载失败: {exc}"], "smoke": None}
    if not resp.get("ok"):
        return {"ok": False, "issues": list(resp.get("issues") or [str(resp.get("error", "沙箱校验失败"))]), "smoke": None}
    return {
        "ok": True,
        "issues": [],
        "smoke": resp.get("smoke"),
    }


def run_position_policy(
    code: str,
    ctx: Any,
    *,
    source_hint: str = "<policy>",
    session: SandboxSession | None = None,
    timeout_s: float = _DEFAULT_POLICY_TIMEOUT_S,
    memory_limit_bytes: int = _POLICY_MEMORY_LIMIT_BYTES,
) -> Any:
    """Execute position policy `compute(ctx)` in sandbox and return PositionSizingResult."""
    from app.backtest.position_sizing import PositionSizingResult

    payload_ctx = {
        "selected": [[c.symbol, float(c.score)] for c in ctx.selected],
        "recent_returns": {str(k): [float(x) for x in v] for k, v in ctx.recent_returns.items()},
        "closed_trades": [
            [t.symbol, float(t.pnl_pct), int(t.duration), str(t.exit_reason)]
            for t in ctx.closed_trades
        ],
        "portfolio": {
            "cash": float(ctx.portfolio.cash),
            "equity": float(ctx.portfolio.equity),
            "drawdown": float(ctx.portfolio.drawdown),
            "open_positions": int(ctx.portfolio.open_positions),
        },
        "params": _json_safe_dict(dict(ctx.params)),
        "api_version": int(getattr(ctx, "api_version", 1)),
    }
    req = {
        "op": "run_position_policy",
        "code": code,
        "source_hint": source_hint,
        "ctx": payload_ctx,
    }
    if session is not None:
        resp, _ = session.request(req, timeout_s=timeout_s)
    else:
        with create_session(
            memory_limit_bytes=memory_limit_bytes,
            cpu_limit_s=int(max(2, timeout_s)),
        ) as sess:
            resp, _ = sess.request(req, timeout_s=timeout_s)
    if not resp.get("ok"):
        _raise_worker_error(resp)
    return PositionSizingResult(
        weights=[float(w) for w in resp["weights"]],
        budget_scale=float(resp.get("budget_scale", 1.0)),
    )


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_json_safe_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, Mapping):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    return str(value)


def _json_safe_dict(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not raw:
        return {}
    return {str(k): _json_safe_value(v) for k, v in raw.items()}


# ================================================================
# Child Worker Runtime (Executed Only in Isolated Subprocess)
# ================================================================

def _install_market_data_rpc_stub(ipc_in: Any, ipc_out: Any) -> None:
    """Replace app.strategy.market_data in child with a read-only IPC stub."""
    stub = types.ModuleType("app.strategy.market_data")

    def _rpc(method: str, args: dict[str, Any]) -> Any:
        _write_frame(ipc_out, {"type": "rpc_market_data", "method": method, "args": args})
        resp_header, resp_atts = _read_frame(ipc_in)
        if not resp_header.get("ok"):
            return [] if method == "list_index_symbols" else pl.DataFrame()
        if resp_header.get("kind") == "json":
            return resp_header.get("value", [])
        return _decode_df(resp_atts["df"][1])

    def _norm_arg_date(val: Any) -> str | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date().isoformat()
        if isinstance(val, date):
            return val.isoformat()
        return str(val)

    def get_index_daily(symbol: Any, start: Any = None, end: Any = None, columns: Any = None) -> pl.DataFrame:
        return _rpc("get_index_daily", {
            "symbol": symbol,
            "start": _norm_arg_date(start),
            "end": _norm_arg_date(end),
            "columns": list(columns) if columns is not None else None,
        })

    def get_etf_daily(symbol: Any, start: Any = None, end: Any = None, columns: Any = None) -> pl.DataFrame:
        return _rpc("get_etf_daily", {
            "symbol": symbol,
            "start": _norm_arg_date(start),
            "end": _norm_arg_date(end),
            "columns": list(columns) if columns is not None else None,
        })

    def get_daily(symbol: Any, start: Any = None, end: Any = None, columns: Any = None) -> pl.DataFrame:
        return _rpc("get_daily", {
            "symbol": symbol,
            "start": _norm_arg_date(start),
            "end": _norm_arg_date(end),
            "columns": list(columns) if columns is not None else None,
        })

    def list_index_symbols() -> list[dict]:
        return list(_rpc("list_index_symbols", {}))

    def get_ext_timeseries(config_id: Any, start: Any = None, end: Any = None, columns: Any = None) -> pl.DataFrame:
        return _rpc("get_ext_timeseries", {
            "config_id": config_id,
            "start": _norm_arg_date(start),
            "end": _norm_arg_date(end),
            "columns": list(columns) if columns is not None else None,
        })

    stub.get_index_daily = get_index_daily  # type: ignore[attr-defined]
    stub.get_etf_daily = get_etf_daily  # type: ignore[attr-defined]
    stub.get_daily = get_daily  # type: ignore[attr-defined]
    stub.list_index_symbols = list_index_symbols  # type: ignore[attr-defined]
    stub.get_ext_timeseries = get_ext_timeseries  # type: ignore[attr-defined]

    # Ensure parent package chain exists cleanly in sys.modules
    importlib.import_module("app.strategy")
    sys.modules["app.strategy.market_data"] = stub


def _build_restricted_builtins(
    allowed_exact_modules: frozenset[str],
    preloaded_deps: Mapping[str, types.ModuleType],
) -> dict[str, Any]:
    from app.strategy.ai_generator import _FORBIDDEN_ATTR_NAMES, _module_attr_names

    def _safe_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        del globals, locals
        if level != 0:
            raise ImportError(f"relative imports are forbidden in sandbox: level={level}")
        if name in preloaded_deps:
            return preloaded_deps[name]
        root = name.split(".", 1)[0]
        allowed = (
            name in allowed_exact_modules
            or (root in _ALLOWED_IMPORT_SUBMODULE_ROOTS and root in allowed_exact_modules)
        )
        if not allowed:
            raise ImportError(f"import {name!r} is forbidden in sandbox")

        if fromlist:
            if "*" in fromlist:
                raise ImportError(f"wildcard import from {name!r} is forbidden")
            mod_attrs = _module_attr_names(name)
            for item in fromlist:
                if item in _FORBIDDEN_ATTR_NAMES or item in mod_attrs or item.startswith("__"):
                    raise ImportError(f"importing {name}.{item} is forbidden in sandbox")

        mod = importlib.import_module(name)
        if not fromlist and "." in name:
            return sys.modules.get(root, mod)
        return mod

    safe: dict[str, Any] = {
        "__build_class__": _py_builtins.__build_class__,
        "__import__": _safe_import,
        "None": None,
        "False": False,
        "True": True,
        "abs": _py_builtins.abs,
        "all": _py_builtins.all,
        "any": _py_builtins.any,
        "bin": _py_builtins.bin,
        "bool": _py_builtins.bool,
        "complex": _py_builtins.complex,
        "dict": _py_builtins.dict,
        "divmod": _py_builtins.divmod,
        "enumerate": _py_builtins.enumerate,
        "filter": _py_builtins.filter,
        "float": _py_builtins.float,
        "frozenset": _py_builtins.frozenset,
        "hash": _py_builtins.hash,
        "hex": _py_builtins.hex,
        "int": _py_builtins.int,
        "isinstance": _py_builtins.isinstance,
        "issubclass": _py_builtins.issubclass,
        "iter": _py_builtins.iter,
        "len": _py_builtins.len,
        "list": _py_builtins.list,
        "map": _py_builtins.map,
        "max": _py_builtins.max,
        "min": _py_builtins.min,
        "next": _py_builtins.next,
        "object": _py_builtins.object,
        "oct": _py_builtins.oct,
        "pow": _py_builtins.pow,
        "property": _py_builtins.property,
        "range": _py_builtins.range,
        "repr": _py_builtins.repr,
        "reversed": _py_builtins.reversed,
        "round": _py_builtins.round,
        "set": _py_builtins.set,
        "slice": _py_builtins.slice,
        "sorted": _py_builtins.sorted,
        "staticmethod": _py_builtins.staticmethod,
        "classmethod": _py_builtins.classmethod,
        "str": _py_builtins.str,
        "sum": _py_builtins.sum,
        "super": _py_builtins.super,
        "tuple": _py_builtins.tuple,
        "zip": _py_builtins.zip,
        "Exception": _py_builtins.Exception,
        "ValueError": _py_builtins.ValueError,
        "TypeError": _py_builtins.TypeError,
        "KeyError": _py_builtins.KeyError,
        "IndexError": _py_builtins.IndexError,
        "RuntimeError": _py_builtins.RuntimeError,
        "StopIteration": _py_builtins.StopIteration,
        "ZeroDivisionError": _py_builtins.ZeroDivisionError,
        "OverflowError": _py_builtins.OverflowError,
        "AssertionError": _py_builtins.AssertionError,
        "AttributeError": _py_builtins.AttributeError,
        "LookupError": _py_builtins.LookupError,
        "ArithmeticError": _py_builtins.ArithmeticError,
    }
    return safe


def _exec_restricted_code(
    code: str,
    *,
    filename: str,
    mod_name: str,
    allowed_exact_modules: frozenset[str],
    preloaded_deps: Mapping[str, types.ModuleType],
) -> types.ModuleType:
    mod = types.ModuleType(mod_name)
    mod.__file__ = filename
    mod.__dict__["__builtins__"] = _build_restricted_builtins(
        allowed_exact_modules,
        preloaded_deps,
    )
    compiled = compile(code, filename, "exec")
    sys.modules[mod_name] = mod
    try:
        exec(compiled, mod.__dict__)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return mod


def _load_strategy_module_in_worker(path: Path) -> tuple[types.ModuleType, str]:
    from app.strategy.ai_generator import AIStrategyGenerator

    dependency_paths = sorted(
        candidate
        for candidate in path.parent.glob("_*.py")
        if candidate != path
    )
    dependency_names = frozenset(candidate.stem for candidate in dependency_paths)
    try:
        code = path.read_text(encoding="utf-8")
        AIStrategyGenerator._validate_safety(
            code,
            extra_allowed_import_modules=dependency_names,
        )
        dep_codes: list[tuple[Path, str]] = []
        for dep_path in dependency_paths:
            dep_code = dep_path.read_text(encoding="utf-8")
            AIStrategyGenerator._validate_safety(
                dep_code,
                extra_allowed_import_modules=_DEP_ALLOWED_EXTRA_MODULES,
            )
            dep_codes.append((dep_path, dep_code))
    except ValueError:
        raise
    except SyntaxError as exc:
        raise ValueError(f"策略语法错误: 第 {exc.lineno} 行, {exc.msg}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"策略安全校验失败: {exc}") from exc

    hasher = hashlib.sha256()
    hasher.update(code.encode("utf-8"))
    for dep_path, dep_code in dep_codes:
        hasher.update(b"\x00")
        hasher.update(dep_path.name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(dep_code.encode("utf-8"))
    source_digest = hasher.hexdigest()

    preloaded_deps: dict[str, types.ModuleType] = {}
    temp_names: list[str] = []
    try:
        for dep_path, dep_code in dep_codes:
            dep_internal_name = f"_tsp_dep_{uuid.uuid4().hex}"
            dep_mod = _exec_restricted_code(
                dep_code,
                filename=str(dep_path),
                mod_name=dep_internal_name,
                allowed_exact_modules=_STRATEGY_ALLOWED_EXACT_MODULES | _DEP_ALLOWED_EXTRA_MODULES,
                preloaded_deps=preloaded_deps,
            )
            preloaded_deps[dep_path.stem] = dep_mod
            temp_names.append(dep_internal_name)

        main_name = f"_tsp_strategy_{uuid.uuid4().hex}"
        temp_names.append(main_name)
        mod = _exec_restricted_code(
            code,
            filename=str(path),
            mod_name=main_name,
            allowed_exact_modules=_STRATEGY_ALLOWED_EXACT_MODULES | dependency_names,
            preloaded_deps=preloaded_deps,
        )
        return mod, source_digest
    finally:
        for tname in temp_names:
            sys.modules.pop(tname, None)


def _worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--app-root", required=True)
    parser.add_argument("--site-dir", action="append", default=[])
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--cpu-limit", type=int, default=120)
    parser.add_argument("--memory-limit", type=int, default=_DEFAULT_MEMORY_LIMIT_BYTES)
    args, _ = parser.parse_known_args(argv)

    # Preserve IPC pipes and isolate sys.stdout/sys.stdin so user `print()` never corrupts frames.
    ipc_in = os.fdopen(os.dup(0), "rb", closefd=True)
    ipc_out = os.fdopen(os.dup(1), "wb", closefd=True)
    devnull_r = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_r, 0)
    os.close(devnull_r)
    os.dup2(2, 1)
    sys.stdin = io.TextIOWrapper(io.FileIO(os.open(os.devnull, os.O_RDONLY), "rb"), encoding="utf-8")
    sys.stdout = sys.stderr

    for sdir in args.site_dir:
        if sdir and Path(sdir).is_dir():
            site.addsitedir(sdir)
    app_root = str(Path(args.app_root).resolve())
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    # Pre-import trusted core libraries BEFORE installing POSIX rlimits/rlimit_nproc
    # and BEFORE running any untrusted strategy/policy code.
    importlib.import_module("app.backtest.matrix")
    importlib.import_module("app.backtest.position_sizing")
    importlib.import_module("app.strategy.ai_generator")

    _install_market_data_rpc_stub(ipc_in, ipc_out)
    _apply_posix_limits(args.cpu_limit, args.memory_limit)

    strategy_cache: dict[str, tuple[types.ModuleType, str]] = {}

    def _get_strategy(path_str: str) -> tuple[types.ModuleType, str]:
        cached = strategy_cache.get(path_str)
        if cached is not None:
            return cached
        loaded = _load_strategy_module_in_worker(Path(path_str))
        strategy_cache[path_str] = loaded
        return loaded

    while True:
        try:
            header, atts = _read_frame(ipc_in)
        except EOFError:
            return 0
        op = str(header.get("op", ""))
        try:
            if op == "inspect_strategy":
                path = Path(str(header["path"]))
                mod, source_digest = _get_strategy(str(path))
                raw_meta = dict(getattr(mod, "META", {}) or {})
                filter_fn = getattr(mod, "filter", None)
                filter_history_fn = getattr(mod, "filter_history", None)
                filter_minute_history_fn = getattr(mod, "filter_minute_history", None)
                matrix_strategy = getattr(mod, "MATRIX_STRATEGY", None)
                module_scoring = getattr(mod, "SCORING", None)
                scoring_columns = (
                    [str(k) for k in module_scoring]
                    if isinstance(module_scoring, dict) and module_scoring
                    else []
                )
                has_exec_backend = hasattr(mod, "EXECUTION_BACKEND")
                exec_backend = (
                    str(mod.EXECUTION_BACKEND)
                    if has_exec_backend
                    else str(
                        raw_meta.get(
                            "execution_backend",
                            "python_history_legacy" if filter_history_fn else "polars_expr",
                        )
                    )
                )
                matrix_info: dict[str, Any] | None = None
                if matrix_strategy is not None:
                    from app.backtest.matrix import MatrixStrategy

                    if exec_backend == "matrix_native" and not isinstance(matrix_strategy, MatrixStrategy):
                        raise TypeError("MATRIX_STRATEGY must implement MatrixStrategy")
                    if isinstance(matrix_strategy, MatrixStrategy):
                        req_fields = sorted(str(f) for f in matrix_strategy.required_fields())
                        has_req_for_params = callable(
                            getattr(matrix_strategy, "required_fields_for_params", None)
                        )
                        raw_scoring = getattr(matrix_strategy, "_scoring", None)
                        raw_dirs = getattr(matrix_strategy, "_directions", None)
                        matrix_info = {
                            "required_fields": req_fields,
                            "has_required_fields_for_params": has_req_for_params,
                            "scoring": _json_safe_dict(raw_scoring) if isinstance(raw_scoring, dict) else None,
                            "directions": _json_safe_dict(raw_dirs) if isinstance(raw_dirs, dict) else None,
                        }

                minute_daily_param_ok = False
                if filter_minute_history_fn is not None and callable(filter_minute_history_fn):
                    sig = inspect.signature(filter_minute_history_fn)
                    minute_daily_param_ok = "daily" in sig.parameters

                data = {
                    "meta": _json_safe_dict(raw_meta),
                    "basic_filter": _json_safe_dict(getattr(mod, "BASIC_FILTER", None)),
                    "entry_signals": _json_safe_value(getattr(mod, "ENTRY_SIGNALS", [])),
                    "exit_signals": _json_safe_value(getattr(mod, "EXIT_SIGNALS", [])),
                    "stop_loss": _json_safe_value(getattr(mod, "STOP_LOSS", None)),
                    "take_profit": _json_safe_value(getattr(mod, "TAKE_PROFIT", None)),
                    "trailing_stop": _json_safe_value(getattr(mod, "TRAILING_STOP", None)),
                    "trailing_take_profit_activate": _json_safe_value(
                        getattr(mod, "TRAILING_TAKE_PROFIT_ACTIVATE", None)
                    ),
                    "trailing_take_profit_drawdown": _json_safe_value(
                        getattr(mod, "TRAILING_TAKE_PROFIT_DRAWDOWN", None)
                    ),
                    "max_hold_days": _json_safe_value(getattr(mod, "MAX_HOLD_DAYS", None)),
                    "lookback_days": _json_safe_value(
                        getattr(mod, "LOOKBACK_DAYS", raw_meta.get("lookback_days", 1))
                    ),
                    "required_features": [
                        str(x)
                        for x in (
                            set(raw_meta.get("required_features", []) or [])
                            | set(getattr(mod, "REQUIRED_FEATURES", []) or [])
                        )
                    ],
                    "scoring_columns": scoring_columns,
                    "execution_backend": exec_backend,
                    "has_filter": filter_fn is not None,
                    "has_filter_history": filter_history_fn is not None,
                    "has_filter_minute_history": filter_minute_history_fn is not None,
                    "has_matrix_strategy": matrix_strategy is not None,
                    "minute_daily_param_ok": minute_daily_param_ok,
                    "matrix_info": matrix_info,
                    "source_digest": source_digest,
                }
                _write_frame(ipc_out, {"type": "result", "ok": True, "data": data})

            elif op == "dry_validate_strategy":
                from app.strategy.ai_generator import AIStrategyGenerator

                code = str(header["code"])
                AIStrategyGenerator._validate_safety(code)
                mod_name = f"_tsp_validate_{uuid.uuid4().hex}"
                try:
                    _exec_restricted_code(
                        code,
                        filename="<ai_validate>",
                        mod_name=mod_name,
                        allowed_exact_modules=_STRATEGY_ALLOWED_EXACT_MODULES,
                        preloaded_deps={},
                    )
                finally:
                    sys.modules.pop(mod_name, None)
                _write_frame(ipc_out, {"type": "result", "ok": True})

            elif op == "run_filter_expr":
                mod, _ = _get_strategy(str(header["path"]))
                df = _decode_df(atts["df"][1])
                params = dict(header.get("params") or {})
                expr = mod.filter(df, params)
                if expr is None:
                    _write_frame(ipc_out, {"type": "result", "ok": True, "is_none": True, "expr_json": None})
                else:
                    if not isinstance(expr, pl.Expr):
                        raise TypeError(f"filter() must return pl.Expr or None, got {type(expr).__name__}")
                    _write_frame(ipc_out, {
                        "type": "result",
                        "ok": True,
                        "is_none": False,
                        "expr_json": expr.meta.serialize(format="json"),
                    })

            elif op == "run_filter_history":
                mod, _ = _get_strategy(str(header["path"]))
                df = _decode_df(atts["df"][1])
                params = dict(header.get("params") or {})
                out = mod.filter_history(df, params)
                if not isinstance(out, pl.DataFrame):
                    raise TypeError(f"filter_history() must return pl.DataFrame, got {type(out).__name__}")
                _write_frame(ipc_out, {"type": "result", "ok": True}, [("df", "arrow", _encode_df(out))])

            elif op == "run_filter_minute_history":
                mod, _ = _get_strategy(str(header["path"]))
                history = _decode_df(atts["history"][1])
                params = dict(header.get("params") or {})
                if header.get("has_daily"):
                    daily = None if header.get("daily_is_none") else _decode_df(atts["daily"][1])
                    out = mod.filter_minute_history(history, params, daily=daily)
                else:
                    out = mod.filter_minute_history(history, params)
                if not isinstance(out, pl.DataFrame):
                    raise TypeError(
                        f"filter_minute_history() must return pl.DataFrame, got {type(out).__name__}"
                    )
                _write_frame(ipc_out, {"type": "result", "ok": True}, [("df", "arrow", _encode_df(out))])

            elif op == "matrix_required_warmup_bars":
                mod, _ = _get_strategy(str(header["path"]))
                params = dict(header.get("params") or {})
                val = int(mod.MATRIX_STRATEGY.required_warmup_bars(params))
                _write_frame(ipc_out, {"type": "result", "ok": True, "value": val})

            elif op == "matrix_required_fields_for_params":
                mod, _ = _get_strategy(str(header["path"]))
                params = dict(header.get("params") or {})
                fn = getattr(mod.MATRIX_STRATEGY, "required_fields_for_params", None)
                vals = sorted(str(x) for x in fn(params)) if callable(fn) else []
                _write_frame(ipc_out, {"type": "result", "ok": True, "value": vals})

            elif op == "matrix_compute_signals":
                from app.backtest.matrix import MarketDataMatrix, validate_signal_matrix

                mod, _ = _get_strategy(str(header["path"]))
                params = dict(header.get("params") or {})
                mmeta = dict(header["market_meta"])
                fields = {
                    fname: _decode_ndarray(atts[f"field:{fname}"][1])
                    for fname in mmeta["field_names"]
                }
                market = MarketDataMatrix(
                    timestamps=_decode_ndarray(atts["timestamps"][1]),
                    timestamp_labels=tuple(mmeta["timestamp_labels"]),
                    session_ids=_decode_ndarray(atts["session_ids"][1]),
                    symbols=tuple(mmeta["symbols"]),
                    names=tuple(mmeta["names"]),
                    open=_decode_ndarray(atts["open"][1]),
                    high=_decode_ndarray(atts["high"][1]),
                    low=_decode_ndarray(atts["low"][1]),
                    close=_decode_ndarray(atts["close"][1]),
                    volume=_decode_ndarray(atts["volume"][1]),
                    tradable=_decode_ndarray(atts["tradable"][1]),
                    limit_up_locked=_decode_ndarray(atts["limit_up_locked"][1]),
                    limit_down_locked=_decode_ndarray(atts["limit_down_locked"][1]),
                    fields=MappingProxyType(fields),
                    vector_fields=frozenset(mmeta.get("vector_fields", ())),
                )
                sig = mod.MATRIX_STRATEGY.compute_signals(market, params)
                validate_signal_matrix(sig, market.shape)
                resp_atts = [
                    ("entry", "npy", _encode_ndarray(sig.entry)),
                    ("exit", "npy", _encode_ndarray(sig.exit)),
                    ("score", "npy", _encode_ndarray(sig.score)),
                    ("entry_signal_code", "npy", _encode_ndarray(sig.entry_signal_code)),
                    ("exit_signal_code", "npy", _encode_ndarray(sig.exit_signal_code)),
                ]
                _write_frame(
                    ipc_out,
                    {
                        "type": "result",
                        "ok": True,
                        "entry_signal_ids": list(sig.entry_signal_ids),
                        "exit_signal_ids": list(sig.exit_signal_ids),
                    },
                    resp_atts,
                )

            elif op == "smoke_position_policy":
                from app.backtest.position_sizing import smoke_compute
                from app.strategy.ai_generator import AIStrategyGenerator

                code = str(header["code"])
                source_hint = str(header.get("source_hint", "<validate>"))
                AIStrategyGenerator._validate_safety(
                    code,
                    extra_allowed_import_modules=frozenset({"app.backtest.position_sizing"}),
                )
                mod_name = f"_tsp_policy_{uuid.uuid4().hex}"
                try:
                    mod = _exec_restricted_code(
                        code,
                        filename=source_hint,
                        mod_name=mod_name,
                        allowed_exact_modules=_POLICY_ALLOWED_EXACT_MODULES,
                        preloaded_deps={},
                    )
                    compute = getattr(mod, "compute", None)
                    if not callable(compute):
                        raise ValueError("compute 不是可调用对象")
                    smoke = smoke_compute(compute)
                finally:
                    sys.modules.pop(mod_name, None)
                if not smoke["ok"]:
                    _write_frame(ipc_out, {
                        "type": "result",
                        "ok": False,
                        "issues": list(smoke["issues"]),
                    })
                else:
                    _write_frame(ipc_out, {"type": "result", "ok": True, "smoke": smoke})

            elif op == "run_position_policy":
                from app.backtest.position_sizing import (
                    Candidate,
                    PortfolioState,
                    PositionSizingContext,
                    PositionSizingResult,
                    TradeSnapshot,
                )
                from app.strategy.ai_generator import AIStrategyGenerator

                code = str(header["code"])
                source_hint = str(header.get("source_hint", "<policy>"))
                cache_key = f"policy:{hashlib.sha256(code.encode('utf-8')).hexdigest()}"
                if cache_key not in strategy_cache:
                    AIStrategyGenerator._validate_safety(
                        code,
                        extra_allowed_import_modules=frozenset({"app.backtest.position_sizing"}),
                    )
                    mod_name = f"_tsp_policy_{uuid.uuid4().hex}"
                    try:
                        mod = _exec_restricted_code(
                            code,
                            filename=source_hint,
                            mod_name=mod_name,
                            allowed_exact_modules=_POLICY_ALLOWED_EXACT_MODULES,
                            preloaded_deps={},
                        )
                    finally:
                        sys.modules.pop(mod_name, None)
                    strategy_cache[cache_key] = (mod, cache_key)
                else:
                    mod, _ = strategy_cache[cache_key]
                compute = getattr(mod, "compute", None)
                if not callable(compute):
                    raise ValueError("compute 不是可调用对象")
                raw_ctx = header["ctx"]
                ctx = PositionSizingContext(
                    selected=tuple(Candidate(str(s), float(sc)) for s, sc in raw_ctx["selected"]),
                    recent_returns=MappingProxyType({
                        str(k): tuple(float(x) for x in v)
                        for k, v in raw_ctx["recent_returns"].items()
                    }),
                    closed_trades=tuple(
                        TradeSnapshot(str(s), float(p), int(d), str(er))
                        for s, p, d, er in raw_ctx["closed_trades"]
                    ),
                    portfolio=PortfolioState(
                        cash=float(raw_ctx["portfolio"]["cash"]),
                        equity=float(raw_ctx["portfolio"]["equity"]),
                        drawdown=float(raw_ctx["portfolio"]["drawdown"]),
                        open_positions=int(raw_ctx["portfolio"]["open_positions"]),
                    ),
                    params=MappingProxyType(dict(raw_ctx["params"])),
                    api_version=int(raw_ctx.get("api_version", 1)),
                )
                res = compute(ctx)
                if not isinstance(res, PositionSizingResult):
                    raise TypeError("compute 必须返回 PositionSizingResult")
                _write_frame(ipc_out, {
                    "type": "result",
                    "ok": True,
                    "weights": [float(w) for w in res.weights],
                    "budget_scale": float(res.budget_scale),
                })

            else:
                raise SandboxError(f"unknown sandbox op: {op!r}")

        except Exception as exc:  # noqa: BLE001
            _write_frame(ipc_out, {
                "type": "result",
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
