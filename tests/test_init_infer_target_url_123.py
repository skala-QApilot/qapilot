"""이슈 #123 P1 — `qapilot init` 의 _infer_target_url 자동 추론 검증.

배경: e2e trace `4e1d8d49` 에서 mini-bss-lite/qapilot.config.yaml 의 target_url
부재로 옵션 C auto-navigate 36회 모두 fail. 사용자가 yaml 직접 편집해야 했던 UX
결함을 자동 추론 + Prompt fallback 으로 해결.

추론 우선순위: vite.config → next.config → package.json dev script → docker-compose
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qapilot.cli.commands.init import _infer_target_url


# ── vite.config — 1순위 ─────────────────────────────────────────────────────


def test_infer_from_vite_config_js(tmp_path: Path):
    """frontend/vite.config.js 의 server.port 추출."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text(
        "import { defineConfig } from 'vite'\n"
        "export default defineConfig({\n"
        "  server: { port: 3000, host: true },\n"
        "})\n"
    )
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:3000"
    assert "vite.config.js" in src


@pytest.mark.parametrize("cfg_name", ["vite.config.ts", "vite.config.mjs", "vite.config.cjs"])
def test_infer_from_vite_config_variants(tmp_path: Path, cfg_name):
    """vite.config 의 다양한 확장자 지원."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / cfg_name).write_text("server: { port: 5173 }")
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:5173"
    assert cfg_name in src


def test_infer_from_web_dir_not_just_frontend(tmp_path: Path):
    """frontend 이외 디렉토리 (web/client/ui/app) 도 탐색."""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "vite.config.js").write_text("server: { port: 4000 }")
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:4000"
    assert src.startswith("web/")


# ── next.config — 2순위 ──────────────────────────────────────────────────────


def test_infer_from_nextjs_default_3000(tmp_path: Path):
    """Next.js 프로젝트 (next.config 존재) → default port 3000."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "next.config.js").write_text("module.exports = {}")
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:3000"
    assert "next.config" in src


# ── package.json dev/start script — 3순위 ──────────────────────────────────


def test_infer_from_package_json_dev_port(tmp_path: Path):
    """frontend/package.json 의 scripts.dev 의 --port 옵션 추출."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        '{"scripts": {"dev": "vite --host 0.0.0.0 --port 8080"}}'
    )
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:8080"
    assert "package.json" in src


def test_infer_from_package_json_start_short_port_opt(tmp_path: Path):
    """scripts.start 의 -p (short option) 도 인식."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        '{"scripts": {"start": "next -p 4321"}}'
    )
    url, _ = _infer_target_url(tmp_path)
    assert url == "http://localhost:4321"


# ── docker-compose — 4순위 (fallback) ──────────────────────────────────────


def test_infer_from_docker_compose_frontend_port(tmp_path: Path):
    """docker-compose.yml 의 frontend service 의 ports → host port 추출."""
    (tmp_path / "docker-compose.yml").write_text("""
services:
  backend:
    image: python:3.11
    ports:
      - "8000:8000"
  frontend:
    image: node:20
    ports:
      - "3000:3000"
    command: npm run dev
""")
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:3000"
    assert "docker-compose.yml" in src


def test_infer_compose_only_if_no_frontend_dir(tmp_path: Path):
    """frontend dir 의 vite.config 가 우선 — compose 보다 신뢰도 ↑."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text("server: { port: 5173 }")
    (tmp_path / "docker-compose.yml").write_text("""
services:
  frontend:
    ports:
      - "3000:3000"
""")
    url, src = _infer_target_url(tmp_path)
    # vite.config 가 우선
    assert url == "http://localhost:5173"
    assert "vite.config.js" in src


# ── 추론 실패 케이스 ─────────────────────────────────────────────────────────


def test_infer_returns_none_for_empty_project(tmp_path: Path):
    """frontend 없음 + compose 없음 → (None, None)."""
    url, src = _infer_target_url(tmp_path)
    assert url is None
    assert src is None


def test_infer_returns_none_when_vite_config_has_no_port(tmp_path: Path):
    """vite.config 에 port 명시 없음 → 다음 후보로 (이 경우 None)."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text(
        "import { defineConfig } from 'vite'\nexport default defineConfig({ plugins: [] })"
    )
    url, _ = _infer_target_url(tmp_path)
    assert url is None


def test_infer_handles_unreadable_file_gracefully(tmp_path: Path, monkeypatch):
    """파일 읽기 실패 시 (OSError) skip + 다음 후보 시도."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text("server: { port: 9999 }")

    # 일부러 read_text raise — 다른 후보 시도되어야
    original_read = Path.read_text
    def mock_read(self, *args, **kwargs):
        if self.name == "vite.config.js":
            raise OSError("permission denied")
        return original_read(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", mock_read)
    # 추가 후보가 없으면 None
    url, _ = _infer_target_url(tmp_path)
    assert url is None


# ── 우선순위 검증 ────────────────────────────────────────────────────────────


def test_priority_vite_beats_package_json(tmp_path: Path):
    """vite.config (1순위) 가 package.json (3순위) 보다 우선."""
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text("server: { port: 5173 }")
    (tmp_path / "frontend" / "package.json").write_text(
        '{"scripts": {"dev": "vite --port 9999"}}'
    )
    url, _ = _infer_target_url(tmp_path)
    assert url == "http://localhost:5173"


def test_priority_frontend_dir_order(tmp_path: Path):
    """frontend / web / client / ui / app 순서 — frontend 가 가장 먼저."""
    # web 과 frontend 둘 다 존재 — frontend 우선
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "vite.config.js").write_text("server: { port: 3000 }")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "vite.config.js").write_text("server: { port: 4000 }")
    url, src = _infer_target_url(tmp_path)
    assert url == "http://localhost:3000"
    assert src.startswith("frontend/")
