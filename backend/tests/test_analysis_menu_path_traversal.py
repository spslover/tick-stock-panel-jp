"""分析菜单删除接口的路径穿越防护 (SEC-001 / CONTRIBUTING §8)。

DELETE /api/analysis-menus/{menu_id} 直接把 menu_id 拼进文件路径后 unlink(),
未做标识符校验与目录包含检查。Windows 下 %5C (反斜杠) 是路径分隔符且不是 URL
路径分隔符, 因此可存活路由匹配, 越出 analysis_menus 目录删除任意 .json
(secrets.json / auth.json / api_tokens.json 等)。

契约:
  - menu_id 仅允许字母、数字、下划线 (与 AnalysisMenu.id 的 pattern 一致);
  - 解析后的目标必须直接位于 analysis_menus 目录内;
  - 拒绝时不得删除任何文件。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import analysis


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(analysis.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _secret(tmp_path: Path) -> Path:
    p = tmp_path / "user_data" / "secrets.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"tickflow_api_key": "x"}', encoding="utf-8")
    return p


def _body() -> dict:
    return {"label": "外部页面", "template": "url", "url": "https://example.com/embed"}


# ---- 穿越载荷: 必须被拒绝且不得删除任何文件 ----


def test_delete_rejects_backslash_traversal_and_keeps_target(tmp_path):
    secret = _secret(tmp_path)
    c = _client(tmp_path)

    # %5C = 反斜杠; Windows 下是路径分隔符, 可越出 analysis_menus
    r = c.delete("/api/analysis-menus/..%5Cuser_data%5Csecrets")

    assert r.status_code == 400, r.text
    assert secret.exists(), "越权删除: secrets.json 被删除"


def test_delete_rejects_forward_slash_traversal_and_keeps_target(tmp_path):
    secret = _secret(tmp_path)
    c = _client(tmp_path)

    r = c.delete("/api/analysis-menus/..%2F..%2Fuser_data%2Fsecrets")

    # %2F 解码后成为 URL 路径分隔符, 路由阶段即不匹配该路由 (404);
    # 无论落到哪一层, 请求都必须被拒绝且不得删除任何文件。
    assert r.status_code >= 400, r.text
    assert secret.exists(), "越权删除: secrets.json 被删除"


def test_delete_rejects_dot_prefix_traversal_and_keeps_target(tmp_path):
    secret = _secret(tmp_path)
    c = _client(tmp_path)

    r = c.delete("/api/analysis-menus/..user_data.secrets")

    assert r.status_code == 400, r.text
    assert secret.exists(), "越权删除: secrets.json 被删除"


def test_delete_rejects_absolute_path_traversal(tmp_path):
    secret = _secret(tmp_path)
    c = _client(tmp_path)

    r = c.delete("/api/analysis-menus/C:%5CWindows%5Csystem.ini")

    assert r.status_code == 400, r.text
    assert secret.exists()


# ---- 正常路径不受影响 ----


def test_normal_delete_still_works(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/analysis-menus/my_embed", json=_body()).status_code == 200

    r = c.delete("/api/analysis-menus/my_embed")

    assert r.status_code == 200, r.text
    assert not (tmp_path / "analysis_menus" / "my_embed.json").exists()


def test_delete_missing_menu_returns_404(tmp_path):
    c = _client(tmp_path)
    assert c.delete("/api/analysis-menus/nope").status_code == 404


def test_upsert_rejects_traversal_id(tmp_path):
    """写入路径与删除路径共用同一标识符契约。"""
    c = _client(tmp_path)
    r = c.post("/api/analysis-menus/..%5Cuser_data%5Csecrets", json=_body())
    assert r.status_code == 400, r.text
    assert not (tmp_path / "user_data" / "secrets.json").exists()
