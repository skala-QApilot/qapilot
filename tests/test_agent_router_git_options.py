"""agent_router._inject_git_options 단위 테스트.

본 helper 가 body 의 GitHub 스캔 필드를 RunOptions 에 주입해야
pipeline._codebase_scan 의 Git/local 분기가 활성화된다.
"""
from __future__ import annotations

from typing import cast

from qapilot.api.agent_router import _inject_git_options
from qapilot.shared.schemas import RunOptions


def _empty_options() -> RunOptions:
    return cast(RunOptions, {
        "command": "generate_scenarios",
        "trigger": "init",
        "user_input": None,
        "scenario_ids": None,
        "filter": None,
        "tags": None,
    })


def test_injects_repo_url_token_branch():
    body = {
        "repo_url": "https://github.com/owner/repo",
        "token": "ghp_xxx",
        "branch": "develop",
    }
    options = _inject_git_options(body, _empty_options())
    assert options.get("repo_url") == "https://github.com/owner/repo"
    assert options.get("token") == "ghp_xxx"
    assert options.get("branch") == "develop"


def test_omits_when_absent():
    body: dict = {}
    options = _inject_git_options(body, _empty_options())
    assert "repo_url" not in options
    assert "token" not in options
    assert "branch" not in options
    assert "repos" not in options


def test_omits_empty_string():
    """빈 문자열은 미입력으로 간주."""
    body = {"repo_url": "", "token": "", "branch": ""}
    options = _inject_git_options(body, _empty_options())
    assert "repo_url" not in options
    assert "token" not in options
    assert "branch" not in options


def test_injects_local_path():
    body = {"local_path": "/path/to/repo"}
    options = _inject_git_options(body, _empty_options())
    assert options.get("local_path") == "/path/to/repo"


def test_injects_repos_list():
    body = {
        "repos": [
            {"repo_url": "https://github.com/owner/a", "token": "t1", "role": "frontend"},
            {"repo_url": "https://github.com/owner/b", "token": "t2", "role": "backend"},
        ]
    }
    options = _inject_git_options(body, _empty_options())
    assert isinstance(options.get("repos"), list)
    assert len(options["repos"]) == 2
    assert options["repos"][0]["role"] == "frontend"


def test_empty_repos_list_not_injected():
    body = {"repos": []}
    options = _inject_git_options(body, _empty_options())
    assert "repos" not in options


def test_non_list_repos_ignored():
    body = {"repos": "not-a-list"}
    options = _inject_git_options(body, _empty_options())
    assert "repos" not in options
