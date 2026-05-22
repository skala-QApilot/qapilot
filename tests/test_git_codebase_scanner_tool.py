"""GitCodebaseScannerTool 단위 테스트."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qapilot.shared.errors import ToolExecutionError
from qapilot.shared.schemas import ToolInput
from qapilot.tools.git_codebase_scanner_tool import (
    GitCodebaseScannerTool,
    GitHubAdapter,
    GitLabAdapter,
    _resolve_adapter,
)


# ── fixtures & helpers ────────────────────────────────────────────────────────

@pytest.fixture
def tool():
    return GitCodebaseScannerTool(trace_id="test-trace-001")


def _make_adapter(
    file_list=None,
    file_content=b"",
    head_hash="abc123",
    commits=None,
    diff=None,
    blame=None,
):
    """모든 메서드가 AsyncMock인 mock 어댑터를 반환한다."""
    adapter = AsyncMock()
    adapter.get_file_list.return_value = file_list or [{"path": "main.py", "type": "blob"}]
    adapter.get_file_content.return_value = file_content
    adapter.get_head_commit_hash.return_value = head_hash
    adapter.get_commits.return_value = commits or [{
        "hash": head_hash,
        "author": "Alice",
        "author_email": "alice@example.com",
        "timestamp": "2026-05-19T00:00:00Z",
    }]
    adapter.get_diff.return_value = diff or []
    adapter.get_blame.return_value = blame or []
    return adapter


def _make_git_diff(**kwargs) -> dict:
    base = {
        "commit_hash": "abc123",
        "prev_hash": "prev456",
        "changed_files": ["main.py"],
        "added_lines": 5,
        "deleted_lines": 2,
        "diff_detail": [{"file": "main.py", "added": 5, "deleted": 2}],
        "author": "Alice",
        "author_email": "alice@example.com",
        "commit_timestamp": "2026-05-19T00:00:00Z",
        "blame": [],
    }
    base.update(kwargs)
    return base


def _make_file_info(path: str, endpoints=None) -> dict:
    return {
        "path": path,
        "language": "python",
        "endpoints": endpoints or [],
        "functions": [],
        "dependencies": [],
        "models": [],
    }


def _make_scan_result(role: str, lang: str = "python", framework: str = "unknown") -> dict:
    return {
        "file_infos": [_make_file_info(f"{role}/app.py")],
        "git_diff": None,
        "language": lang,
        "framework": framework,
    }


# ── _normalize_repos ──────────────────────────────────────────────────────────

def test_normalize_repos_single_explicit_role():
    params = {"repo_url": "https://github.com/org/backend", "token": "tok", "role": "api"}
    repos = GitCodebaseScannerTool._normalize_repos(params)
    assert len(repos) == 1
    assert repos[0]["role"] == "api"
    assert repos[0]["branch"] == "main"


def test_normalize_repos_single_auto_role_from_url():
    params = {"repo_url": "https://github.com/org/my-service", "token": "tok"}
    repos = GitCodebaseScannerTool._normalize_repos(params)
    assert repos[0]["role"] == "my-service"


def test_normalize_repos_single_strips_git_suffix():
    params = {"repo_url": "https://github.com/org/repo.git"}
    repos = GitCodebaseScannerTool._normalize_repos(params)
    assert repos[0]["role"] == "repo"


def test_normalize_repos_multi_explicit_roles():
    params = {
        "repos": [
            {"repo_url": "https://github.com/org/be", "token": "t1", "role": "backend"},
            {"repo_url": "https://github.com/org/fe", "token": "t2", "role": "frontend"},
        ]
    }
    repos = GitCodebaseScannerTool._normalize_repos(params)
    assert len(repos) == 2
    assert repos[0]["role"] == "backend"
    assert repos[1]["role"] == "frontend"


def test_normalize_repos_multi_auto_role():
    params = {
        "repos": [
            {"repo_url": "https://github.com/org/backend"},
            {"repo_url": "https://gitlab.example.com/org/frontend"},
        ]
    }
    repos = GitCodebaseScannerTool._normalize_repos(params)
    assert repos[0]["role"] == "backend"
    assert repos[1]["role"] == "frontend"


# ── _execute: skip ────────────────────────────────────────────────────────────

async def test_execute_doc_update_returns_skip(tool):
    result = await tool._execute({"trigger": "doc_update", "repo_url": "https://github.com/org/r"})
    assert result["scan_result"] is None
    assert result["_metadata"]["skipped"] is True
    assert result["_metadata"]["reason"] == "doc_update"


async def test_execute_all_repos_skipped_returns_skip(tool):
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(return_value={"skipped": True})):
        result = await tool._execute({
            "trigger": "natural_lang",
            "repo_url": "https://github.com/org/repo",
            "token": "tok",
        })
    assert result["scan_result"] is None
    assert result["_metadata"]["reason"] == "no_code_change"


async def test_execute_all_repos_fail_raises(tool):
    with patch.object(
        tool, "_scan_single_repo",
        new=AsyncMock(side_effect=ToolExecutionError("TOOL_001", "network error")),
    ):
        with pytest.raises(ToolExecutionError, match="모든 레포 스캔 실패"):
            await tool._execute({
                "trigger": "init",
                "repo_url": "https://github.com/org/repo",
                "token": "tok",
            })


# ── _execute: happy path ──────────────────────────────────────────────────────

async def test_execute_single_repo_scan_result_shape(tool):
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(return_value=_make_scan_result("repo", "python", "fastapi"))):
        output = await tool._execute({
            "trigger": "init",
            "repo_url": "https://github.com/org/repo",
            "token": "tok",
        })

    sr = output["scan_result"]
    assert sr["language"] == "repo:python"
    assert sr["framework"] == "repo:fastapi"
    assert sr["endpoint_count"] == 0
    assert output["_metadata"]["repos_scanned"] == 1
    assert output["_metadata"]["files_scanned"] == 1


async def test_execute_multi_repo_merges_files_and_languages(tool):
    results = [
        {**_make_scan_result("backend", "python", "fastapi"), "file_infos": [_make_file_info("backend/app.py", endpoints=[{"method": "GET", "path": "/"}])]},
        {**_make_scan_result("frontend", "typescript", "nextjs"), "file_infos": [_make_file_info("frontend/page.ts")]},
    ]
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(side_effect=results)):
        output = await tool._execute({
            "trigger": "init",
            "repos": [
                {"repo_url": "https://github.com/org/backend", "token": "t1", "role": "backend"},
                {"repo_url": "https://github.com/org/frontend", "token": "t2", "role": "frontend"},
            ],
        })

    sr = output["scan_result"]
    assert len(sr["files"]) == 2
    assert sr["endpoint_count"] == 1
    assert "backend:python" in sr["language"]
    assert "frontend:typescript" in sr["language"]
    assert output["_metadata"]["repos_scanned"] == 2


async def test_execute_partial_failure_uses_successful_repos(tool):
    """일부 레포 실패 시 성공한 레포 결과로 ScanResult를 구성한다."""
    with patch.object(
        tool, "_scan_single_repo",
        new=AsyncMock(side_effect=[
            ToolExecutionError("TOOL_001", "fail"),
            _make_scan_result("frontend", "typescript"),
        ]),
    ):
        output = await tool._execute({
            "trigger": "init",
            "repos": [
                {"repo_url": "https://github.com/org/backend", "token": "t1", "role": "backend"},
                {"repo_url": "https://github.com/org/frontend", "token": "t2", "role": "frontend"},
            ],
        })

    assert output["scan_result"] is not None
    assert output["_metadata"]["repos_scanned"] == 1  # 성공한 레포 수만 집계


async def test_execute_git_diffs_merged(tool):
    d1 = _make_git_diff(commit_hash="aaa", prev_hash="000", added_lines=3, deleted_lines=1)
    d2 = _make_git_diff(commit_hash="bbb", prev_hash="111", added_lines=7, deleted_lines=2)
    results = [
        {**_make_scan_result("be"), "git_diff": d1},
        {**_make_scan_result("fe"), "git_diff": d2},
    ]
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(side_effect=results)):
        output = await tool._execute({
            "trigger": "init",
            "repos": [
                {"repo_url": "https://github.com/org/be", "token": "t1", "role": "be"},
                {"repo_url": "https://github.com/org/fe", "token": "t2", "role": "fe"},
            ],
        })

    gd = output["scan_result"]["git_diff"]
    assert gd["commit_hash"] == "aaa,bbb"
    assert gd["added_lines"] == 10


# ── _scan_single_repo ─────────────────────────────────────────────────────────

async def test_scan_single_repo_role_prefix_on_path(tool):
    adapter = _make_adapter(file_list=[{"path": "src/main.py", "type": "blob"}])
    with patch("qapilot.tools.git_codebase_scanner_tool._resolve_adapter", return_value=adapter), \
         patch.object(tool, "_detect_language_framework", new=AsyncMock(return_value=("python", "fastapi"))), \
         patch.object(tool, "_collect_files", new=AsyncMock(return_value=["src/main.py"])), \
         patch.object(tool, "_extract_git_diff", new=AsyncMock(return_value=None)):

        result = await tool._scan_single_repo(
            {"repo_url": "https://github.com/org/repo", "token": "tok", "branch": "main", "role": "backend"},
            "init",
            None,
        )

    assert result["file_infos"][0]["path"] == "backend/src/main.py"
    assert result["language"] == "python"
    assert result["framework"] == "fastapi"


async def test_scan_single_repo_natural_lang_same_hash_skips(tool):
    adapter = _make_adapter(head_hash="same-hash")
    with patch("qapilot.tools.git_codebase_scanner_tool._resolve_adapter", return_value=adapter):
        result = await tool._scan_single_repo(
            {"repo_url": "https://github.com/org/repo", "token": "tok", "branch": "main", "role": "api"},
            "natural_lang",
            "same-hash",
        )
    assert result.get("skipped") is True


async def test_scan_single_repo_file_fetch_failure_skipped(tool):
    """파일 fetch 실패 시 해당 파일만 건너뛰고 나머지는 정상 반환한다."""
    adapter = _make_adapter(file_list=[
        {"path": "ok.py", "type": "blob"},
        {"path": "bad.py", "type": "blob"},
    ])
    adapter.get_file_content.side_effect = [
        b"print('ok')",
        ToolExecutionError("TOOL_001", "fetch fail"),
    ]
    with patch("qapilot.tools.git_codebase_scanner_tool._resolve_adapter", return_value=adapter), \
         patch.object(tool, "_detect_language_framework", new=AsyncMock(return_value=("python", "unknown"))), \
         patch.object(tool, "_collect_files", new=AsyncMock(return_value=["ok.py", "bad.py"])), \
         patch.object(tool, "_extract_git_diff", new=AsyncMock(return_value=None)):

        result = await tool._scan_single_repo(
            {"repo_url": "https://github.com/org/repo", "token": "tok", "branch": "main", "role": "svc"},
            "init",
            None,
        )

    assert len(result["file_infos"]) == 1
    assert result["file_infos"][0]["path"] == "svc/ok.py"


# ── _merge_git_diffs ──────────────────────────────────────────────────────────

def test_merge_git_diffs_sums_lines():
    d1 = _make_git_diff(commit_hash="aaa", prev_hash="000", added_lines=3, deleted_lines=1, changed_files=["a.py"], diff_detail=[], blame=[])
    d2 = _make_git_diff(commit_hash="bbb", prev_hash="111", added_lines=7, deleted_lines=2, changed_files=["b.py"], diff_detail=[], blame=[])
    merged = GitCodebaseScannerTool._merge_git_diffs([d1, d2])
    assert merged["commit_hash"] == "aaa,bbb"
    assert merged["prev_hash"] == "000,111"
    assert merged["added_lines"] == 10
    assert merged["deleted_lines"] == 3
    assert set(merged["changed_files"]) == {"a.py", "b.py"}


def test_merge_git_diffs_author_from_first():
    d1 = _make_git_diff(author="Alice", commit_hash="a1", prev_hash="")
    d2 = _make_git_diff(author="Bob", commit_hash="b2", prev_hash="")
    merged = GitCodebaseScannerTool._merge_git_diffs([d1, d2])
    assert merged["author"] == "Alice"


def test_merge_git_diffs_blame_concatenated():
    blame1 = [{"file": "a.py", "author": "Alice", "author_email": "", "timestamp": ""}]
    blame2 = [{"file": "b.py", "author": "Bob", "author_email": "", "timestamp": ""}]
    d1 = _make_git_diff(blame=blame1)
    d2 = _make_git_diff(blame=blame2)
    merged = GitCodebaseScannerTool._merge_git_diffs([d1, d2])
    assert len(merged["blame"]) == 2


# ── _check_skip ───────────────────────────────────────────────────────────────

async def test_check_skip_doc_update(tool):
    result, _ = await tool._check_skip(AsyncMock(), "doc_update", "main", "")
    assert result["_metadata"]["reason"] == "doc_update"


async def test_check_skip_natural_lang_same_hash(tool):
    adapter = AsyncMock()
    adapter.get_head_commit_hash.return_value = "abc"
    result, trigger = await tool._check_skip(adapter, "natural_lang", "main", "abc")
    assert result is not None
    assert result["_metadata"]["reason"] == "no_code_change"


async def test_check_skip_natural_lang_different_hash(tool):
    adapter = AsyncMock()
    adapter.get_head_commit_hash.return_value = "new-hash"
    result, trigger = await tool._check_skip(adapter, "natural_lang", "main", "old-hash")
    assert result is None
    assert trigger == "code_change"


async def test_check_skip_natural_lang_no_last_hash_proceeds(tool):
    """last_commit_hash 없으면 API 호출 없이 code_change로 진행한다."""
    adapter = AsyncMock()
    result, trigger = await tool._check_skip(adapter, "natural_lang", "main", "")
    assert result is None
    assert trigger == "code_change"
    adapter.get_head_commit_hash.assert_not_called()


async def test_check_skip_init_passes_through(tool):
    result, trigger = await tool._check_skip(AsyncMock(), "init", "main", "")
    assert result is None
    assert trigger == "init"


# ── _should_scan_path ─────────────────────────────────────────────────────────

def test_should_scan_path_python(tool):
    assert tool._should_scan_path("src/main.py") is True


def test_should_scan_path_typescript(tool):
    assert tool._should_scan_path("src/app.ts") is True


def test_should_scan_path_config_file(tool):
    assert tool._should_scan_path(".env") is True


def test_should_scan_path_excludes_node_modules(tool):
    assert tool._should_scan_path("node_modules/lib/index.js") is False


def test_should_scan_path_excludes_test_files(tool):
    assert tool._should_scan_path("tests/test_main.py") is False


def test_should_scan_path_excludes_spec_files(tool):
    assert tool._should_scan_path("src/app.spec.ts") is False


def test_should_scan_path_excludes_unknown_extension(tool):
    assert tool._should_scan_path("README.md") is False


# ── _resolve_adapter ──────────────────────────────────────────────────────────

def test_resolve_adapter_returns_github_for_github_url():
    assert isinstance(_resolve_adapter("https://github.com/org/repo", "tok"), GitHubAdapter)


def test_resolve_adapter_returns_gitlab_for_custom_host():
    assert isinstance(_resolve_adapter("https://gitlab.example.com/org/repo", "tok"), GitLabAdapter)


def test_resolve_adapter_returns_gitlab_for_non_github():
    assert isinstance(_resolve_adapter("https://bitbucket.example.com/org/repo", "tok"), GitLabAdapter)


# ── GitHubAdapter ─────────────────────────────────────────────────────────────

async def test_github_get_blame_returns_empty_list():
    adapter = GitHubAdapter("https://github.com/org/repo", "tok")
    assert await adapter.get_blame("src/main.py", "main") == []


async def test_github_get_file_list_filters_blobs():
    adapter = GitHubAdapter("https://github.com/org/repo", "tok")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "tree": [
            {"path": "main.py", "type": "blob"},
            {"path": "src/", "type": "tree"},
            {"path": "utils.py", "type": "blob"},
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await adapter.get_file_list("main")

    assert len(result) == 2
    assert all(item["type"] == "blob" for item in result)


async def test_github_get_head_commit_hash():
    adapter = GitHubAdapter("https://github.com/org/repo", "tok")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"sha": "deadbeef"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await adapter.get_head_commit_hash("main")

    assert result == "deadbeef"


# ── GitLabAdapter ─────────────────────────────────────────────────────────────

async def test_gitlab_get_blame_returns_last_commit():
    adapter = GitLabAdapter("https://gitlab.example.com/org/repo", "tok")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [
        {"commit": {"author_name": "Alice", "author_email": "alice@example.com", "authored_date": "2026-05-19T00:00:00Z"}, "lines": ["line1"]},
        {"commit": {"author_name": "Bob", "author_email": "bob@example.com", "authored_date": "2026-05-20T00:00:00Z"}, "lines": ["line2"]},
    ]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await adapter.get_blame("src/main.py", "main")

    assert len(result) == 1
    assert result[0]["author"] == "Bob"  # 마지막 blame 항목
    assert result[0]["file"] == "src/main.py"


async def test_gitlab_get_diff_parses_unified_diff():
    adapter = GitLabAdapter("https://gitlab.example.com/org/repo", "tok")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "diffs": [
            {"new_path": "main.py", "old_path": "main.py", "diff": "+added\n-deleted\n context\n"}
        ]
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.get.return_value = mock_resp

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await adapter.get_diff("base", "head")

    assert len(result) == 1
    assert result[0]["file"] == "main.py"
    assert result[0]["added"] == 1
    assert result[0]["deleted"] == 1


async def test_gitlab_project_id_encoded():
    """org/repo 경로가 URL 인코딩되어 API 호출에 사용된다."""
    adapter = GitLabAdapter("https://gitlab.example.com/org/my-repo", "tok")
    assert adapter._project_id == "org%2Fmy-repo"


# ── #161 수정 항목 테스트 ──────────────────────────────────────────────────────

# 1. framework 감지 버그
async def test_detect_python_framework_uses_full_path(tool):
    """all_paths가 전체 경로일 때 main.py를 정확히 찾아 fastapi를 감지한다."""
    adapter = AsyncMock()
    adapter.get_file_content.return_value = b"from fastapi import FastAPI\napp = FastAPI()"
    all_paths = ["backend/app/main.py", "backend/app/config.py"]
    result = await tool._detect_python_framework(adapter, "main", all_paths)
    assert result == "fastapi"
    adapter.get_file_content.assert_called_once_with("backend/app/main.py", "main")


async def test_detect_python_framework_returns_unknown_when_no_entry(tool):
    """main.py / app.py가 없으면 unknown을 반환한다."""
    adapter = AsyncMock()
    result = await tool._detect_python_framework(adapter, "main", ["backend/utils.py"])
    assert result == "unknown"
    adapter.get_file_content.assert_not_called()


# 2. scan_status 필드
async def test_execute_metadata_has_scan_status_full(tool):
    """trigger=init이면 scan_status가 full이다."""
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(return_value={
        "file_infos": [{"path": "r/a.py", "language": "python", "endpoints": [],
                        "functions": [], "dependencies": [], "models": []}],
        "git_diff": None, "language": "python", "framework": "fastapi",
    })):
        output = await tool._execute({"trigger": "init", "repo_url": "https://github.com/o/r", "token": "t"})
    assert output["_metadata"]["scan_status"] == "full"


async def test_execute_metadata_has_scan_status_incremental(tool):
    """trigger=code_change + last_commit_hash 있으면 scan_status가 incremental이다."""
    with patch.object(tool, "_scan_single_repo", new=AsyncMock(return_value={
        "file_infos": [{"path": "r/a.py", "language": "python", "endpoints": [],
                        "functions": [], "dependencies": [], "models": []}],
        "git_diff": None, "language": "python", "framework": "fastapi",
    })):
        output = await tool._execute({
            "trigger": "code_change",
            "repo_url": "https://github.com/o/r",
            "token": "t",
            "last_commit_hash": "abc123",
        })
    assert output["_metadata"]["scan_status"] == "incremental"


# 3. endpoint 스키마
def test_extract_py_endpoint_has_required_fields(tool):
    """Python endpoint 추출 결과에 file·params·response_model 필드가 있다."""
    from tree_sitter import Language, Parser
    import tree_sitter_python as tspython

    src = b"""
@router.post("/signup", response_model=TokenOut)
def signup(body: SignupRequest, db: Session = Depends(get_db)):
    pass
"""
    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(src)
    endpoints, _, _ = tool._parse_python(tree)
    assert len(endpoints) == 1
    ep = endpoints[0]
    assert ep["method"] == "POST"
    assert ep["path"] == "/signup"
    assert "params" in ep
    assert isinstance(ep["params"], list)
    assert "response_model" in ep
    assert "TokenOut" in ep["response_model"]


def test_parse_file_injects_file_field(tool):
    """_parse_file_from_content 후 endpoint와 function에 file 필드가 주입된다."""
    src = b"""
@router.get("/items")
def list_items():
    pass
"""
    fi = tool._parse_file_from_content("role/app/routers/items.py", "python", src)
    for ep in fi["endpoints"]:
        assert ep["file"] == "role/app/routers/items.py"
    for fn in fi["functions"]:
        assert fn["file"] == "role/app/routers/items.py"


# 4. function params list
def test_extract_py_function_params_is_list(tool):
    """Python 함수 params가 list[str]로 반환된다."""
    from tree_sitter import Language, Parser
    import tree_sitter_python as tspython

    src = b"def my_func(self, x: int, y: str = 'hello'):\n    pass\n"
    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(src)
    _, functions, _ = tool._parse_python(tree)
    assert len(functions) == 1
    assert isinstance(functions[0]["params"], list)
    assert len(functions[0]["params"]) > 0


# 5. 라우터 prefix
async def test_build_router_prefix_map(tool):
    """main.py의 include_router 호출에서 prefix 맵을 추출한다."""
    main_py = b"""
from fastapi import FastAPI
from app.routers import auth, orders

app = FastAPI()
app.include_router(auth.router, prefix="/api/auth")
app.include_router(orders.router, prefix="/api/orders")
"""
    adapter = AsyncMock()
    adapter.get_file_content.return_value = main_py
    all_paths = ["app/main.py", "app/routers/auth.py", "app/routers/orders.py"]
    prefix_map = await tool._build_router_prefix_map(adapter, "main", all_paths)
    assert prefix_map.get("auth") == "/api/auth"
    assert prefix_map.get("orders") == "/api/orders"


@pytest.mark.asyncio
async def test_build_router_prefix_map_alias(tool):
    """from X import Y as Z alias 패턴에서도 prefix를 올바르게 추출한다."""
    main_py = b"""
from fastapi import FastAPI
from app.routers import router as contracts_router

app = FastAPI()
app.include_router(contracts_router, prefix="/api/contracts")
"""
    adapter = AsyncMock()
    adapter.get_file_content.return_value = main_py
    all_paths = ["app/main.py", "app/routers.py"]
    prefix_map = await tool._build_router_prefix_map(adapter, "main", all_paths)
    assert prefix_map.get("routers") == "/api/contracts"


def test_apply_router_prefixes_updates_endpoint_paths(tool):
    """prefix_map이 있으면 해당 파일의 endpoint path에 prefix가 붙는다."""
    file_infos = [
        {"path": "role/app/routers/auth.py", "language": "python",
         "endpoints": [{"method": "POST", "path": "/login", "handler": "login",
                        "params": [], "response_model": "", "file": "role/app/routers/auth.py"}],
         "functions": [], "dependencies": [], "models": []},
        {"path": "role/app/routers/auth.py", "language": "python",
         "endpoints": [{"method": "GET", "path": "", "handler": "me",
                        "params": [], "response_model": "", "file": "role/app/routers/auth.py"}],
         "functions": [], "dependencies": [], "models": []},
    ]
    tool._apply_router_prefixes(file_infos, {"auth": "/api/auth"})
    assert file_infos[0]["endpoints"][0]["path"] == "/api/auth/login"
    assert file_infos[1]["endpoints"][0]["path"] == "/api/auth"


# Git Diff — HEAD~1 fallback
async def test_extract_git_diff_uses_parent_when_no_last_hash(tool):
    """last_commit_hash 없으면 HEAD~1을 prev_hash로 사용해 diff를 계산한다."""
    adapter = _make_adapter(
        head_hash="head111",
        commits=[
            {"hash": "head111", "author": "Alice", "author_email": "a@x.com", "timestamp": "2026-05-22T00:00:00Z"},
            {"hash": "parent000", "author": "Bob", "author_email": "b@x.com", "timestamp": "2026-05-21T00:00:00Z"},
        ],
        diff=[{"file": "app/main.py", "added": 5, "deleted": 2}],
    )
    result = await tool._extract_git_diff(adapter, "main", "")
    assert result is not None
    assert result["prev_hash"] == "parent000"
    assert result["changed_files"] == ["app/main.py"]
    assert result["added_lines"] == 5
    assert result["deleted_lines"] == 2
    adapter.get_diff.assert_called_once_with("parent000", "head111")


async def test_extract_git_diff_uses_last_hash_when_provided(tool):
    """last_commit_hash가 있으면 그것을 기준으로 diff를 계산한다."""
    adapter = _make_adapter(
        head_hash="head111",
        commits=[
            {"hash": "head111", "author": "Alice", "author_email": "a@x.com", "timestamp": "2026-05-22T00:00:00Z"},
            {"hash": "parent000", "author": "Bob", "author_email": "b@x.com", "timestamp": "2026-05-21T00:00:00Z"},
        ],
        diff=[{"file": "app/config.py", "added": 3, "deleted": 1}],
    )
    result = await tool._extract_git_diff(adapter, "main", "custom_base_hash")
    assert result["prev_hash"] == "custom_base_hash"
    adapter.get_diff.assert_called_once_with("custom_base_hash", "head111")


async def test_extract_git_diff_empty_prev_when_single_commit(tool):
    """커밋이 1개뿐이면 prev_hash가 빈 문자열이고 diff도 없다."""
    adapter = _make_adapter(
        head_hash="first111",
        commits=[
            {"hash": "first111", "author": "Alice", "author_email": "a@x.com", "timestamp": "2026-05-22T00:00:00Z"},
        ],
    )
    result = await tool._extract_git_diff(adapter, "main", "")
    assert result is not None
    assert result["prev_hash"] == ""
    assert result["changed_files"] == []
    adapter.get_diff.assert_not_called()


# 6. alembic 제외
def test_should_scan_path_excludes_alembic(tool):
    """alembic 디렉토리 하위 파일은 스캔 대상에서 제외된다."""
    assert tool._should_scan_path("backend/alembic/env.py") is False
    assert tool._should_scan_path("backend/alembic/versions/0001_init.py") is False
