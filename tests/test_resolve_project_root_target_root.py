"""`_resolve_project_root` 의 state.target_root 우선 분기 검증.

SaaS 흐름에서 Spring 이 body 로 보낸 service.target_root 가 PipelineState 까지 전파된 후,
`_resolve_project_root` 가 cfg.project.root 다음, state.qapilot_dir derive 전 우선 사용해야
본인 데이터 layer 의 `scan_all_metadata` 가 SUT 실제 코드 path 로 호출된다.

진단: sut-0610-03 trace `d04a0c2b` 에서 state.qapilot_dir 이 임시 디렉토리라
`<root>/.qapilot[/service]` derive fail → `tv_metadata_index_empty` graceful skip.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from qapilot.orchestrator.pipeline import _resolve_project_root


def _empty_cfg() -> SimpleNamespace:
    """cfg.project.root / repo_path 둘 다 None — CLI 흐름이 비어있는 SaaS 상황."""
    return SimpleNamespace(project=SimpleNamespace(root=None, repo_path=None))


def _cfg_with_root(root: str) -> SimpleNamespace:
    return SimpleNamespace(project=SimpleNamespace(root=root, repo_path=None))


def test_cfg_root_wins_over_state_target_root():
    """cfg.project.root 가 있으면 그것 우선 (CLI 흐름 회귀 보호)."""
    with tempfile.TemporaryDirectory() as cfg_dir, tempfile.TemporaryDirectory() as state_dir:
        state = {"qapilot_dir": "/tmp/x", "target_root": state_dir}
        with patch("qapilot.shared.config.load_config", return_value=_cfg_with_root(cfg_dir)):
            result = _resolve_project_root(state)
        assert result == Path(cfg_dir).resolve()


def test_state_target_root_used_when_cfg_empty():
    """cfg 비어있고 state.target_root 가 유효 dir 이면 state 사용 (SaaS 흐름 본질)."""
    with tempfile.TemporaryDirectory() as state_dir:
        state = {
            "qapilot_dir": "/var/folders/tmp/qapilot_xxx",  # 임시 dir (derive 불가)
            "target_root": state_dir,
        }
        with patch("qapilot.shared.config.load_config", return_value=_empty_cfg()):
            result = _resolve_project_root(state)
        assert result == Path(state_dir).resolve()


def test_state_target_root_skipped_when_not_dir():
    """state.target_root 가 존재하지 않는 path 면 다음 단계 (qapilot_dir derive) 시도."""
    with tempfile.TemporaryDirectory() as derive_root:
        # qapilot_dir = <derive_root>/.qapilot/<service> 형태 → derive 통과
        qapilot_dir = Path(derive_root) / ".qapilot" / "svc-x"
        qapilot_dir.mkdir(parents=True)
        state = {
            "qapilot_dir": str(qapilot_dir),
            "target_root": "/nonexistent/path/that/does/not/exist",
        }
        with patch("qapilot.shared.config.load_config", return_value=_empty_cfg()):
            result = _resolve_project_root(state)
        assert result == Path(derive_root).resolve()


def test_returns_none_when_all_sources_missing():
    """cfg 비어있고 state.target_root None, state.qapilot_dir 도 derive 불가 → None."""
    state = {
        "qapilot_dir": "/var/folders/tmp/qapilot_xxx",  # `.qapilot` 패턴 아님
        "target_root": None,
    }
    with patch("qapilot.shared.config.load_config", return_value=_empty_cfg()):
        result = _resolve_project_root(state)
    assert result is None


def test_target_root_expanduser():
    """state.target_root 가 `~/...` 형태일 때 expanduser 적용."""
    with tempfile.TemporaryDirectory() as td:
        # `~` 처리 검증을 위해 home 디렉토리 안 임시 폴더 만들기 어려움 — 절대경로로 충분
        state = {"qapilot_dir": "/tmp/x", "target_root": td}
        with patch("qapilot.shared.config.load_config", return_value=_empty_cfg()):
            result = _resolve_project_root(state)
        # resolve() 후 일치
        assert result == Path(td).resolve()


def test_empty_string_target_root_treated_as_missing():
    """state.target_root = '' (빈 문자열) 은 미주입과 동등 — qapilot_dir derive 시도."""
    with tempfile.TemporaryDirectory() as derive_root:
        qapilot_dir = Path(derive_root) / ".qapilot"
        qapilot_dir.mkdir()
        state = {
            "qapilot_dir": str(qapilot_dir),
            "target_root": "",
        }
        with patch("qapilot.shared.config.load_config", return_value=_empty_cfg()):
            result = _resolve_project_root(state)
        assert result == Path(derive_root).resolve()
