"""Isolated E2E test harness for backend/tests/e2e_prod.

Provides:
- Custom pytest marker registration (`r2_post_fix`, `r3_post_fix`, `e2e_subprocess`)
- Complete `DATA_DIR` / `.env` / `.builtin_presets_seeded` isolation per test
- Full 20+ singleton/cache reset and restore so `e2e_prod` coexists cleanly with all other tests
- `e2e_client` (`TestClient` with `127.0.0.1` peer) & `make_e2e_client` factory
- `uvicorn_server` ASGI subprocess launcher on an ephemeral `127.0.0.1` port
- `mock_provider_factory` for all 7 datasets without live network calls
- `local_http_server` thread-backed `127.0.0.1` HTTP server for pull/webhook/redirect tests
"""
from __future__ import annotations

import atexit
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import polars as pl
import pytest
from fastapi.testclient import TestClient

# Guard import-time side effects BEFORE any `app.*` module is imported when running e2e_prod standalone.
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
_BOOT_TMP_DIR: Path | None = None

if "app.config" not in sys.modules:
    _BOOT_TMP_DIR = Path(tempfile.mkdtemp(prefix="tsp_e2e_boot_"))
    (_BOOT_TMP_DIR / "ext_data").mkdir(parents=True, exist_ok=True)
    (_BOOT_TMP_DIR / "ext_data" / ".builtin_presets_seeded").write_text("1", encoding="utf-8")
    (_BOOT_TMP_DIR / ".env").write_text("", encoding="utf-8")
    os.environ.setdefault("DATA_DIR", str(_BOOT_TMP_DIR))
    os.environ.setdefault("TICKFLOW_ENV_FILE", str(_BOOT_TMP_DIR / ".env"))
    os.environ.setdefault("TIERS_YAML", str(_REPO_ROOT / "tiers.yaml"))
    os.environ.setdefault("TICKFLOW_API_KEY", "")
    os.environ.setdefault("AI_API_KEY", "")
    os.environ.setdefault("WATCHDOG_ENABLED", "false")
    os.environ.setdefault("BACKTEST_MATRIX_CACHE_PREWARM", "false")
    os.environ.setdefault("TICK_HEAVY_MIN_AVAILABLE_BYTES", "0")


def _cleanup_boot_dir() -> None:
    if _BOOT_TMP_DIR is not None and _BOOT_TMP_DIR.exists():
        shutil.rmtree(_BOOT_TMP_DIR, ignore_errors=True)


atexit.register(_cleanup_boot_dir)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "r2_post_fix: marks tests validating R2 remediation contracts (financial accuracy, security, SSRF, isolation)",
    )
    config.addinivalue_line(
        "markers",
        "r3_post_fix: marks tests validating R3 production observability/readiness/audit/hardening contracts",
    )
    config.addinivalue_line(
        "markers",
        "e2e_subprocess: marks tests that spawn a real Uvicorn ASGI subprocess",
    )


def preseed_data_dir(data_dir: Path) -> Path:
    """Create minimal isolated DATA_DIR structure and block startup live network preset pulls."""
    data_dir.mkdir(parents=True, exist_ok=True)
    ext_dir = data_dir / "ext_data"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / ".builtin_presets_seeded").write_text("1", encoding="utf-8")
    return data_dir


def reset_all_singletons(data_dir: Path) -> None:
    """Reset all known module-level singletons and caches to point cleanly at `data_dir`."""
    from app import secrets_store
    from app.api import auth as auth_api
    from app.api import data as data_api
    from app.api import overview as overview_api
    from app.api import regime as regime_api
    from app.config import settings
    from app.data_providers.custom import loader as custom_loader
    from app.factors import ext_factors
    from app.indicators import pipeline as ind_pipeline
    from app.services import (
        api_gateway,
        api_tokens,
        event_tickets,
        ext_data,
        pipeline_jobs,
        preferences,
        strategy_run_queue,
        trading_day,
        workload,
    )
    from app.services import auth as auth_service
    from app.tickflow import client as tf_client
    from app.tickflow import rate_limits as tf_rate_limits

    settings.data_dir = data_dir
    settings.tickflow_api_key = ""
    settings.ai_api_key = ""
    settings.watchdog_enabled = False
    settings.backtest_matrix_cache_prewarm = False

    tf_client.reset_clients()

    secrets_store._cache = None
    secrets_store._cache_sig = None

    preferences._invalidate_cache()

    with auth_service._lock:
        auth_service._sessions.clear()
        auth_service._configured_cache = None
        auth_service._fail_counter.clear()
        auth_service._fail_loaded_path = None
    auth_service.invalidate_runtime_caches()
    if hasattr(auth_api, "_fail_counter") and isinstance(auth_api._fail_counter, dict):
        auth_api._fail_counter.clear()

    api_tokens.clear_cache()
    api_tokens._last_persist.clear()

    api_gateway.clear_rate_limit_buckets()
    api_gateway._BUCKETS.clear()
    api_gateway.compile_trusted_cidrs.cache_clear()

    pipeline_jobs.job_store = pipeline_jobs.JobStore(store_dir=data_dir / "job_store")
    pipeline_jobs._CANCEL_FLAGS.clear()
    pipeline_jobs.release_run_slot(None)

    trading_day.reset_cache()
    ext_data._load_all_cache.clear()
    ext_factors.invalidate_ext_caches(data_dir)

    overview_api.invalidate_overview_cache()
    regime_api.invalidate_regime_cache()
    data_api.invalidate_data_cache(None, discard_stale=True)

    ind_pipeline.invalidate_custom_signals()
    ind_pipeline._benchmark_cache.clear()

    workload.workload = workload.WorkloadStats()
    workload.submissions = workload.SubmissionLimiter()

    strategy_run_queue.MANAGER = strategy_run_queue.StrategyRunManager()

    custom_loader.load_all(data_dir / "data_sources")
    tf_rate_limits._next_slot.clear()

    event_tickets._TICKETS.clear()

    try:
        from app.custom.assistant import sessions as assistant_sessions

        assistant_sessions._cache = None
        assistant_sessions._cache_sig = None
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.tickflow.policy import detect_capabilities

        detect_capabilities(force=True)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def isolated_data_dir(tmp_path: Path) -> Path:
    """Isolated DATA_DIR pre-seeded to prevent any startup network pulls."""
    data_dir = preseed_data_dir(tmp_path / "data")
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    return data_dir


@pytest.fixture
def make_e2e_client(
    isolated_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Callable[..., TestClient], None, None]:
    """Factory creating TestClient(app) with full settings/env/singleton isolation and teardown."""
    from app.config import settings
    from app.main import app

    saved = {
        "data_dir": settings.data_dir,
        "tickflow_api_key": settings.tickflow_api_key,
        "ai_api_key": settings.ai_api_key,
        "watchdog_enabled": settings.watchdog_enabled,
        "backtest_matrix_cache_prewarm": settings.backtest_matrix_cache_prewarm,
        "auth_password": settings.auth_password,
        "open_api_cors_origins": settings.open_api_cors_origins,
        "trusted_proxy_cidrs": settings.trusted_proxy_cidrs,
        "auth_cookie_secure": settings.auth_cookie_secure,
        "ai_daily_token_budget": settings.ai_daily_token_budget,
        "static_dir": settings.static_dir,
    }

    monkeypatch.setenv("DATA_DIR", str(isolated_data_dir))
    monkeypatch.setenv("TICKFLOW_ENV_FILE", str(isolated_data_dir.parent / ".env"))
    monkeypatch.setenv("TICKFLOW_API_KEY", "")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("WATCHDOG_ENABLED", "false")
    monkeypatch.setenv("BACKTEST_MATRIX_CACHE_PREWARM", "false")
    monkeypatch.setenv("TICK_HEAVY_MIN_AVAILABLE_BYTES", "0")

    active_clients: list[TestClient] = []

    def _factory(
        *,
        client_host: str = "127.0.0.1",
        client_port: int = 50000,
        settings_overrides: dict[str, Any] | None = None,
        data_dir: Path | None = None,
        raise_server_exceptions: bool = True,
    ) -> TestClient:
        target_dir = preseed_data_dir(data_dir or isolated_data_dir)
        monkeypatch.setenv("DATA_DIR", str(target_dir))
        reset_all_singletons(target_dir)

        settings.auth_password = ""
        settings.open_api_cors_origins = ""
        settings.trusted_proxy_cidrs = "127.0.0.1/32,::1/128"
        settings.auth_cookie_secure = None
        settings.ai_daily_token_budget = 500_000
        if settings_overrides:
            for k, v in settings_overrides.items():
                setattr(settings, k, v)

        tc = TestClient(
            app,
            client=(client_host, client_port),
            raise_server_exceptions=raise_server_exceptions,
        )
        tc.__enter__()
        active_clients.append(tc)
        return tc

    yield _factory

    for tc in reversed(active_clients):
        try:
            tc.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    for k, v in saved.items():
        setattr(settings, k, v)
    reset_all_singletons(saved["data_dir"])


@pytest.fixture
def e2e_client(make_e2e_client: Callable[..., TestClient]) -> TestClient:
    """Default isolated TestClient bound to `isolated_data_dir` with `127.0.0.1` peer."""
    return make_e2e_client()


class MockMarketProvider:
    """Deterministic fake provider supporting all 7 datasets without live network."""

    def __init__(
        self,
        name: str = "mock_provider",
        *,
        datasets: set[str] | None = None,
        daily_df: pl.DataFrame | None = None,
        adj_factor_df: pl.DataFrame | None = None,
        minute_df: pl.DataFrame | None = None,
        realtime_df: pl.DataFrame | None = None,
        realtime_indices: list[dict[str, Any]] | None = None,
        depth_map: dict[str, dict[str, Any]] | None = None,
        financial_tables: dict[str, pl.DataFrame] | None = None,
        instruments_by_type: dict[str, pl.DataFrame] | None = None,
        fail_datasets: dict[str, Exception] | None = None,
    ) -> None:
        from app.data_providers.base import ProviderCapabilities

        self.name = name
        self.datasets = datasets if datasets is not None else {
            "daily", "adj_factor", "minute", "full_minute", "realtime", "depth5", "financial",
        }
        self.capabilities = ProviderCapabilities(
            instruments=True,
            daily="daily" in self.datasets,
            adj_factor="adj_factor" in self.datasets,
            minute="minute" in self.datasets,
            realtime="realtime" in self.datasets,
            depth5="depth5" in self.datasets,
            financial="financial" in self.datasets,
        )
        self.daily_df = daily_df if daily_df is not None else pl.DataFrame()
        self.adj_factor_df = adj_factor_df if adj_factor_df is not None else pl.DataFrame()
        self.minute_df = minute_df if minute_df is not None else pl.DataFrame()
        self.realtime_df = realtime_df if realtime_df is not None else pl.DataFrame()
        self.realtime_indices = realtime_indices
        self.depth_map = depth_map if depth_map is not None else {}
        self.financial_tables = financial_tables if financial_tables is not None else {}
        self.instruments_by_type = instruments_by_type if instruments_by_type is not None else {}
        self.fail_datasets = fail_datasets if fail_datasets is not None else {}
        self.calls: list[tuple[str, Any]] = []

    def _maybe_raise(self, dataset: str) -> None:
        if dataset in self.fail_datasets:
            raise self.fail_datasets[dataset]

    def get_instruments(self, asset_type: str = "stock") -> pl.DataFrame:
        self.calls.append(("get_instruments", asset_type))
        return self.instruments_by_type.get(asset_type, pl.DataFrame())

    def get_daily(self, symbols: list[str], start_time: Any = None, end_time: Any = None, asset_type: str = "stock") -> pl.DataFrame:
        self.calls.append(("get_daily", (list(symbols), asset_type)))
        self._maybe_raise("daily")
        df = self.daily_df
        if df.is_empty() or not symbols:
            return df
        return df.filter(pl.col("symbol").is_in(symbols))

    def get_adj_factors(self, symbols: list[str], start_time: Any = None, end_time: Any = None, asset_type: str = "stock") -> pl.DataFrame:
        self.calls.append(("get_adj_factors", (list(symbols), asset_type)))
        self._maybe_raise("adj_factor")
        df = self.adj_factor_df
        if df.is_empty() or not symbols:
            return df
        return df.filter(pl.col("symbol").is_in(symbols))

    def get_minute(
        self,
        symbols: list[str],
        start_time: Any = None,
        end_time: Any = None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        self.calls.append(("get_minute", (list(symbols), asset_type, freq)))
        self._maybe_raise("minute")
        if on_chunk_done is not None:
            on_chunk_done(1, 1)
        df = self.minute_df
        if df.is_empty() or not symbols:
            return df
        return df.filter(pl.col("symbol").is_in(symbols))

    def get_realtime(self, universes: list[str] | None = None, symbols: list[str] | None = None) -> pl.DataFrame:
        self.calls.append(("get_realtime", (universes, symbols)))
        self._maybe_raise("realtime")
        df = self.realtime_df
        if not df.is_empty() and symbols:
            return df.filter(pl.col("symbol").is_in(symbols))
        return df

    def get_realtime_indices(self, symbols: list[str]) -> list[dict[str, Any]] | None:
        self.calls.append(("get_realtime_indices", list(symbols)))
        self._maybe_raise("realtime_indices")
        return self.realtime_indices

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.calls.append(("get_depth_batch", list(symbols)))
        self._maybe_raise("depth5")
        return {s: self.depth_map[s] for s in symbols if s in self.depth_map}

    def get_financials(self, table: str, symbols: list[str] | None = None, latest_only: bool = False) -> pl.DataFrame:
        self.calls.append(("get_financials", (table, symbols, latest_only)))
        self._maybe_raise("financial")
        df = self.financial_tables.get(table, pl.DataFrame())
        if not df.is_empty() and symbols and "symbol" in df.columns:
            return df.filter(pl.col("symbol").is_in(symbols))
        return df

    def get_intraday_batch(self, symbols: list[str], date: Any = None) -> pl.DataFrame:
        self.calls.append(("get_intraday_batch", (list(symbols), date)))
        self._maybe_raise("full_minute")
        return self.minute_df

    def get_intraday_latest(self, symbols: list[str]) -> pl.DataFrame:
        self.calls.append(("get_intraday_latest", list(symbols)))
        self._maybe_raise("full_minute")
        return self.minute_df

    def close(self) -> None:
        return None


@pytest.fixture
def mock_provider_factory() -> Generator[Callable[..., MockMarketProvider], None, None]:
    """Register deterministic MockMarketProvider instances into custom.loader._PROVIDERS and clean up."""
    from app.data_providers.custom import loader as custom_loader
    from app.data_providers.custom.managed import ManagedProvider

    registered: list[str] = []

    def _register(
        name: str = "mock_src",
        *,
        display_name: str = "Mock Source",
        datasets: set[str] | None = None,
        **kwargs: Any,
    ) -> MockMarketProvider:
        provider = MockMarketProvider(name=name, datasets=datasets, **kwargs)
        ds_map = {ds: SimpleNamespace(endpoint=f"/mock/{ds}") for ds in provider.datasets}
        provider.config = SimpleNamespace(  # type: ignore[attr-defined]
            name=name,
            display_name=display_name,
            datasets=ds_map,
            path=None,
            homepage="",
            key_manage_url="",
            api_key_env="",
        )
        managed = ManagedProvider(provider, builtin=False)
        custom_loader._PROVIDERS[name] = managed
        registered.append(name)
        return provider

    yield _register

    for name in registered:
        custom_loader._PROVIDERS.pop(name, None)


class LocalHTTPServer:
    """Thread-backed local HTTP server on 127.0.0.1 for YAML pull / webhook / redirect tests."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]] = {}
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        parent = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                parsed = urlsplit(self.path)
                with parent._lock:
                    parent.requests.append({
                        "method": method,
                        "path": parsed.path,
                        "query": parsed.query,
                        "full_path": self.path,
                        "headers": headers,
                        "body": body,
                    })
                    handler = parent.routes.get(parsed.path) or parent.routes.get("*")

                if handler is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"detail":"not found"}')
                    return

                status, resp_headers, payload = handler(self.path, headers, body)
                self.send_response(status)
                for k, v in resp_headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def set_json_route(self, path: str, payload: Any, *, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def _handler(_full_path: str, _headers: dict[str, str], _body: bytes) -> tuple[int, dict[str, str], bytes]:
            return status, {"Content-Type": "application/json"}, raw

        with self._lock:
            self.routes[path] = _handler

    def set_redirect_route(self, path: str, location: str, *, status: int = 302) -> None:
        def _handler(_full_path: str, _headers: dict[str, str], _body: bytes) -> tuple[int, dict[str, str], bytes]:
            return status, {"Location": location}, b""

        with self._lock:
            self.routes[path] = _handler

    def set_route(
        self,
        path: str,
        fn: Callable[[str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]],
    ) -> None:
        with self._lock:
            self.routes[path] = fn

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3.0)


@pytest.fixture
def local_http_server() -> Generator[LocalHTTPServer, None, None]:
    srv = LocalHTTPServer()
    try:
        yield srv
    finally:
        srv.close()


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class UvicornProcess:
    """Managed real Uvicorn ASGI subprocess bound to an isolated 127.0.0.1 port."""

    def __init__(self, data_dir: Path, env_overrides: dict[str, str] | None = None) -> None:
        self.data_dir = preseed_data_dir(data_dir)
        self.port = _free_local_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env_file = self.data_dir.parent / ".env.subproc"
        if not env_file.exists():
            env_file.write_text("", encoding="utf-8")

        env = os.environ.copy()
        env.update({
            "DATA_DIR": str(self.data_dir),
            "TICKFLOW_ENV_FILE": str(env_file),
            "TIERS_YAML": str(_REPO_ROOT / "tiers.yaml"),
            "TICKFLOW_API_KEY": "",
            "AI_API_KEY": "",
            "WATCHDOG_ENABLED": "false",
            "BACKTEST_MATRIX_CACHE_PREWARM": "false",
            "TICK_HEAVY_MIN_AVAILABLE_BYTES": "0",
            "PYTHONUTF8": "1",
        })
        if env_overrides:
            env.update(env_overrides)

        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                "1",
                "--log-level",
                "warning",
            ],
            cwd=str(_BACKEND_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def wait_ready(self, timeout_s: float = 30.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                out, err = self.proc.communicate(timeout=2.0)
                raise RuntimeError(
                    f"Uvicorn exited prematurely (rc={self.proc.returncode}): stdout={out!r} stderr={err!r}"
                )
            try:
                resp = httpx.get(f"{self.base_url}/health", timeout=1.5, trust_env=False)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.2)
        raise TimeoutError(f"Uvicorn did not become healthy within {timeout_s}s: {last_err}")

    def wait_indicators_ready(self, timeout_s: float = 20.0, cookies: dict[str, str] | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(
                    f"{self.base_url}/api/data/status",
                    cookies=cookies,
                    timeout=1.5,
                    trust_env=False,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("indicators_ready") is True:
                        return body
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        raise TimeoutError(f"indicators_ready did not become True within {timeout_s}s")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5.0)
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001
                    pass


@pytest.fixture
def uvicorn_server(isolated_data_dir: Path) -> Generator[Callable[..., UvicornProcess], None, None]:
    """Launcher for real Uvicorn ASGI subprocesses with automatic clean teardown."""
    procs: list[UvicornProcess] = []

    def _start(
        *,
        data_dir: Path | None = None,
        env_overrides: dict[str, str] | None = None,
        wait: bool = True,
        timeout_s: float = 30.0,
    ) -> UvicornProcess:
        up = UvicornProcess(data_dir or isolated_data_dir, env_overrides=env_overrides)
        procs.append(up)
        if wait:
            up.wait_ready(timeout_s=timeout_s)
        return up

    yield _start

    for up in reversed(procs):
        up.stop()


@functools.lru_cache(maxsize=1)
def backend_dir() -> Path:
    return _BACKEND_DIR
