"""Git REST API 기반 코드베이스 스캔 Tool.

프로젝트 소스코드를 Git REST API(GitHub / GitLab)를 통해 분석하여
API 엔드포인트, 데이터 모델, 프레임워크, Git 이력을 추출한다.
로컬 클론 없이 메모리 내에서 처리하며, manifest는 RDB(pipeline.py 담당)로 관리한다.

담당: C
Created: 2026-05-21
"""

import asyncio
import base64
import fnmatch
import json
import re
from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjs
import tree_sitter_python as tspython
import tree_sitter_typescript as tsts
import tree_sitter_yaml as tsyaml
from tree_sitter import Language, Node, Parser

from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.shared.schemas import FileInfo, GitDiff, ScanResult
from qapilot.tools.base_tool import BaseTool

# ── 상수 ──────────────────────────────────────────────────────────────────────

_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", "__pycache__",
    "dist", "build", "venv", ".venv",
    ".qapilot", ".pytest_cache",
    "alembic",
})
_EXCLUDE_PATTERNS = frozenset({
    "*.min.js", "*.lock",
    "test_*.py", "*_test.py",
    "*.spec.ts", "*.spec.js", "*.test.ts", "*.test.js",
    "webpack.config.js", "vite.config.ts", "*.config.js", "*.config.ts",
})
_CONFIG_FILES = frozenset({
    "application.yml", "application.properties",
    ".env", ".env.example", "docker-compose.yml",
})
_SENSITIVE_KEYS = frozenset({"password", "secret", "key", "token", "credential"})
_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".java": "java",
    ".yml": "yaml", ".yaml": "yaml",
}
_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch"})
_HTTP_ROUTE_ATTRS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
_JAVA_MAPPINGS: dict[str, str | None] = {
    "RequestMapping": None,
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_MODEL_ANNS = frozenset({"Entity", "Data", "Table", "Document", "Embeddable"})
_ENV_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_PROP_RE = re.compile(r"^([^#][^=]*)=(.*)$")
_STAT_RE = re.compile(r"(\d+) insertions?\(\+\).*?(\d+) deletions?\(-\)", re.DOTALL)


# ── 모듈 수준 헬퍼 ─────────────────────────────────────────────────────────────

def _find_nodes(node: Node, type_name: str) -> list[Node]:
    """트리에서 주어진 타입의 모든 노드를 재귀 탐색한다."""
    results: list[Node] = []
    if node.type == type_name:
        results.append(node)
    for child in node.children:
        results.extend(_find_nodes(child, type_name))
    return results


def _node_text(node: Node | None) -> str:
    """노드의 텍스트를 UTF-8 문자열로 반환한다."""
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _params_to_list(params_node: Node | None) -> list[str]:
    """파라미터 노드에서 개별 파라미터 문자열 목록을 반환한다."""
    if params_node is None:
        return []
    return [
        _node_text(child).strip()
        for child in params_node.named_children
        if _node_text(child).strip() not in ("", "(", ")", ",")
    ]


def _count_diff_lines(diff_text: str) -> tuple[int, int]:
    """unified diff 텍스트에서 추가/삭제 라인 수를 반환한다."""
    added = sum(
        1 for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    deleted = sum(
        1 for line in diff_text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return added, deleted


# ── Git 플랫폼 어댑터 ──────────────────────────────────────────────────────────

class GitPlatformAdapter(ABC):
    """Git 플랫폼 REST API 추상 어댑터."""

    @abstractmethod
    async def get_file_list(self, branch: str) -> list[dict]:
        """저장소 파일 목록을 반환한다.

        Returns:
            [{"path": "src/main.py", "type": "blob"}, ...]
        """

    @abstractmethod
    async def get_file_content(self, file_path: str, branch: str) -> bytes:
        """단일 파일 내용을 bytes로 반환한다."""

    @abstractmethod
    async def get_head_commit_hash(self, branch: str) -> str:
        """현재 HEAD commit hash를 반환한다."""

    @abstractmethod
    async def get_commits(self, branch: str, since_hash: str | None = None) -> list[dict]:
        """커밋 목록을 반환한다.

        Returns:
            [{"hash": "...", "author": "...", "author_email": "...", "timestamp": "..."}]
        """

    @abstractmethod
    async def get_diff(self, base_hash: str, head_hash: str) -> list[dict]:
        """두 커밋 간 파일별 변경 내역을 반환한다.

        Returns:
            [{"file": "...", "added": int, "deleted": int}]
        """

    @abstractmethod
    async def get_blame(self, file_path: str, branch: str) -> list[dict]:
        """파일의 blame 요약을 반환한다.

        Returns:
            [{"file": "...", "author": "...", "author_email": "...", "timestamp": "..."}]
        """


class GitHubAdapter(GitPlatformAdapter):
    """GitHub REST API 어댑터."""

    _BASE = "https://api.github.com"

    def __init__(self, repo_url: str, token: str) -> None:
        parts = repo_url.rstrip("/").split("/")
        self._owner = parts[-2]
        self._repo = parts[-1].removesuffix(".git")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{self._BASE}/repos/{self._owner}/{self._repo}{path}"

    async def get_file_list(self, branch: str) -> list[dict]:
        url = self._url(f"/git/trees/{branch}?recursive=1")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            data = resp.json()
            return [
                {"path": item["path"], "type": item["type"]}
                for item in data.get("tree", [])
                if item["type"] == "blob"
            ]

    async def get_file_content(self, file_path: str, branch: str) -> bytes:
        url = self._url(f"/contents/{file_path}?ref={branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            data = resp.json()
            return base64.b64decode(data["content"].replace("\n", ""))

    async def get_head_commit_hash(self, branch: str) -> str:
        url = self._url(f"/commits/{branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return resp.json()["sha"]

    async def get_commits(self, branch: str, since_hash: str | None = None) -> list[dict]:
        url = self._url(f"/commits?sha={branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return [
                {
                    "hash": item["sha"],
                    "author": item["commit"]["author"]["name"],
                    "author_email": item["commit"]["author"]["email"],
                    "timestamp": item["commit"]["author"]["date"],
                }
                for item in resp.json()
            ]

    async def get_diff(self, base_hash: str, head_hash: str) -> list[dict]:
        url = self._url(f"/compare/{base_hash}...{head_hash}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return [
                {
                    "file": f["filename"],
                    "added": f["additions"],
                    "deleted": f["deletions"],
                }
                for f in resp.json().get("files", [])
            ]

    async def get_blame(self, file_path: str, branch: str) -> list[dict]:
        # GitHub REST API는 blame 미지원
        return []


class GitLabAdapter(GitPlatformAdapter):
    """GitLab REST API 어댑터. 사내 GitLab 포함."""

    def __init__(self, repo_url: str, token: str) -> None:
        u = urlparse(repo_url.rstrip("/"))
        self._base_url = f"{u.scheme}://{u.netloc}"
        self._project_path = u.path.lstrip("/").removesuffix(".git")
        self._project_id = quote(self._project_path, safe="")
        self._headers = {"PRIVATE-TOKEN": token}

    def _api(self, path: str) -> str:
        return f"{self._base_url}/api/v4/projects/{self._project_id}{path}"

    async def get_file_list(self, branch: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            while True:
                resp = await client.get(
                    self._api("/repository/tree"),
                    params={"recursive": "true", "ref": branch, "per_page": 100, "page": page},
                )
                _raise_for_api_error(resp)
                batch = resp.json()
                items.extend(
                    {"path": item["path"], "type": item["type"]}
                    for item in batch
                    if item["type"] == "blob"
                )
                if len(batch) < 100:
                    break
                page += 1
        return items

    async def get_file_content(self, file_path: str, branch: str) -> bytes:
        encoded = quote(file_path, safe="")
        url = self._api(f"/repository/files/{encoded}/raw?ref={branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return resp.content

    async def get_head_commit_hash(self, branch: str) -> str:
        url = self._api(f"/repository/commits/{branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return resp.json()["id"]

    async def get_commits(self, branch: str, since_hash: str | None = None) -> list[dict]:
        url = self._api(f"/repository/commits?ref_name={branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            return [
                {
                    "hash": item["id"],
                    "author": item["author_name"],
                    "author_email": item["author_email"],
                    "timestamp": item["authored_date"],
                }
                for item in resp.json()
            ]

    async def get_diff(self, base_hash: str, head_hash: str) -> list[dict]:
        url = self._api(f"/repository/compare?from={base_hash}&to={head_hash}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            result: list[dict] = []
            for d in resp.json().get("diffs", []):
                added, deleted = _count_diff_lines(d.get("diff", ""))
                result.append({
                    "file": d.get("new_path") or d.get("old_path", ""),
                    "added": added,
                    "deleted": deleted,
                })
            return result

    async def get_blame(self, file_path: str, branch: str) -> list[dict]:
        encoded = quote(file_path, safe="")
        url = self._api(f"/repository/files/{encoded}/blame?ref={branch}")
        async with httpx.AsyncClient(headers=self._headers, timeout=30.0) as client:
            resp = await client.get(url)
            _raise_for_api_error(resp)
            entries = resp.json()
            if not entries:
                return []
            latest = entries[-1]["commit"]
            return [{
                "file": file_path,
                "author": latest.get("author_name", ""),
                "author_email": latest.get("author_email", ""),
                "timestamp": latest.get("authored_date", ""),
            }]


def _raise_for_api_error(resp: httpx.Response) -> None:
    """HTTP 오류 응답을 ToolExecutionError로 변환한다."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ToolExecutionError(
            ErrorCode.TOOL_001,
            f"Git API 호출 실패: {e.response.status_code} {e.request.url}",
        ) from e


def _resolve_adapter(repo_url: str, token: str) -> GitPlatformAdapter:
    """repo_url에 따라 적합한 어댑터를 반환한다."""
    if "github.com" in repo_url:
        return GitHubAdapter(repo_url, token)
    return GitLabAdapter(repo_url, token)


# ── Tool ───────────────────────────────────────────────────────────────────────

class GitCodebaseScannerTool(BaseTool):
    """Git REST API 기반 코드베이스 스캔 Tool.

    역할: REST API로 소스코드를 메모리 내 분석, 엔드포인트/함수/모델 추출
    단일 레포(repo_url)와 멀티 레포(repos) 입력을 모두 지원한다.
    멀티 레포는 asyncio.gather로 병렬 스캔하며, FileInfo.path에 role/ prefix를 붙인다.
    출력: {"scan_result": ScanResult}
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._parsers: dict[str, Parser] = {}

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Git REST API로 코드베이스를 스캔하고 ScanResult를 반환한다.

        Args:
            params: 단일 레포 또는 멀티 레포 형식 (아래 참고).

                단일 레포::

                    {
                        "trigger":           "init" | "code_change" | "doc_update" | "natural_lang",
                        "repo_url":          str,           # Git 저장소 URL
                        "token":             str,           # read-only 접근 토큰
                        "branch":            str = "main",  # 스캔 대상 브랜치
                        "last_commit_hash":  str = "",      # 증분 스캔 기준 hash (선택)
                    }

                멀티 레포::

                    {
                        "trigger": "init" | "code_change" | "doc_update" | "natural_lang",
                        "repos": [
                            {
                                "repo_url": str,
                                "token":    str,
                                "branch":   str = "main",
                                "role":     str = "<레포명>",  # FileInfo.path prefix, 미입력 시 URL에서 추출
                            },
                            ...
                        ],
                        "last_commit_hash": str = "",  # 모든 레포 공통 기준 hash (선택)
                    }

        Returns:
            {"scan_result": ScanResult, "_metadata": {...}}

        Raises:
            ToolExecutionError: 모든 레포 스캔 실패 시.
        """
        trigger: str = params.get("trigger", "init")

        if trigger == "doc_update":
            return {"scan_result": None, "_metadata": {"skipped": True, "reason": "doc_update"}}

        repos = self._normalize_repos(params)
        last_commit_hash: str | None = params.get("last_commit_hash")

        results = await asyncio.gather(
            *[self._scan_single_repo(repo, trigger, last_commit_hash) for repo in repos],
            return_exceptions=True,
        )

        file_infos_all: list[FileInfo] = []
        git_diffs: list[GitDiff] = []
        languages: list[str] = []
        frameworks: list[str] = []
        skipped_count = 0
        scan_errors: list[str] = []

        for repo, result in zip(repos, results):
            if isinstance(result, BaseException):
                err_msg = str(result)
                self.logger.warning(
                    "repo_scan_failed", repo=repo["repo_url"], error=err_msg
                )
                scan_errors.append(f"{repo['repo_url']}: {err_msg}")
                continue
            if result.get("skipped"):
                skipped_count += 1
                continue
            file_infos_all.extend(result["file_infos"])
            if result["git_diff"]:
                git_diffs.append(result["git_diff"])
            languages.append(f"{repo['role']}:{result['language']}")
            frameworks.append(f"{repo['role']}:{result['framework']}")

        if not file_infos_all:
            if skipped_count == len(repos):
                return {"scan_result": None, "_metadata": {"skipped": True, "reason": "no_code_change"}}
            detail = "; ".join(scan_errors) if scan_errors else "파일 없음"
            raise ToolExecutionError(ErrorCode.TOOL_001, f"모든 레포 스캔 실패: {detail}")

        scan_result: ScanResult = {
            "files": file_infos_all,
            "git_diff": self._merge_git_diffs(git_diffs) if git_diffs else None,
            "framework": ",".join(frameworks),
            "language": ",".join(languages),
            "endpoint_count": sum(len(fi["endpoints"]) for fi in file_infos_all),
        }
        scan_status = (
            "incremental"
            if trigger in ("code_change", "natural_lang") and last_commit_hash
            else "full"
        )
        self.logger.info(
            "scan_complete",
            files=len(file_infos_all),
            endpoints=scan_result["endpoint_count"],
            repos=len(repos),
        )
        return {
            "scan_result": scan_result,
            "_metadata": {
                "trigger": trigger,
                "scan_status": scan_status,
                "files_scanned": len(file_infos_all),
                "repos_scanned": len([r for r in results if not isinstance(r, BaseException)]),
            },
        }

    # ── 멀티 레포 지원 ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_repos(params: dict[str, Any]) -> list[dict]:
        """단일/멀티 레포 params를 통일된 내부 포맷으로 정규화한다.

        role 미입력 시 repo_url에서 레포명을 추출하여 자동 할당한다.
        """
        def _default_role(repo_url: str) -> str:
            return PurePosixPath(repo_url.rstrip("/")).name.removesuffix(".git")

        if "repo_url" in params:
            repo_url = params["repo_url"]
            return [{
                "repo_url": repo_url,
                "token": params.get("token", ""),
                "branch": params.get("branch", "main"),
                "role": params.get("role") or _default_role(repo_url),
            }]
        return [
            {
                "repo_url": r["repo_url"],
                "token": r.get("token", ""),
                "branch": r.get("branch", "main"),
                "role": r.get("role") or _default_role(r["repo_url"]),
            }
            for r in params["repos"]
        ]

    async def _scan_single_repo(
        self,
        repo: dict,
        trigger: str,
        last_commit_hash: str | None,
    ) -> dict:
        """단일 레포를 스캔하여 분석 결과를 반환한다.

        Args:
            repo: {"repo_url", "token", "branch", "role"}
            trigger: 스캔 트리거.
            last_commit_hash: 이전 commit hash (증분 스캔용).

        Returns:
            {"file_infos", "git_diff", "language", "framework"}
            또는 {"skipped": True} (natural_lang 트리거 + 변경 없음 시).
        """
        repo_url = repo["repo_url"]
        branch = repo.get("branch", "main")
        role = repo["role"]
        last_hash = last_commit_hash or ""

        adapter = _resolve_adapter(repo_url, repo.get("token", ""))

        skip_result, effective_trigger = await self._check_skip(
            adapter, trigger, branch, last_hash
        )
        if skip_result is not None:
            return {"skipped": True}

        head_hash = await adapter.get_head_commit_hash(branch)
        all_files_raw = await adapter.get_file_list(branch)
        all_paths = [f["path"] for f in all_files_raw]

        language, framework = await self._detect_language_framework(adapter, branch, all_paths)
        files_to_scan = await self._collect_files(
            adapter, effective_trigger, branch, last_hash, head_hash
        )

        file_infos: list[FileInfo] = []
        for file_path in files_to_scan:
            try:
                content = await adapter.get_file_content(file_path, branch)
            except ToolExecutionError as e:
                self.logger.warning("file_fetch_failed", path=file_path, error=str(e))
                continue
            file_lang = _LANG_MAP.get(PurePosixPath(file_path).suffix.lower(), "unknown")
            prefixed_path = f"{role}/{file_path}"
            fi = self._parse_file_from_content(prefixed_path, file_lang, content)
            file_infos.append(fi)

        if language == "python":
            prefix_map = await self._build_router_prefix_map(adapter, branch, all_paths)
            if prefix_map:
                self._apply_router_prefixes(file_infos, prefix_map)

        git_diff = await self._extract_git_diff(adapter, branch, last_hash)

        self.logger.info(
            "repo_scan_complete",
            role=role,
            files=len(file_infos),
            language=language,
            framework=framework,
        )
        return {
            "file_infos": file_infos,
            "git_diff": git_diff,
            "language": language,
            "framework": framework,
        }

    @staticmethod
    def _merge_git_diffs(diffs: list[GitDiff]) -> GitDiff:
        """여러 레포의 GitDiff를 하나로 합친다.

        commit_hash / prev_hash는 ',' 구분으로 이어 붙이고,
        파일 목록과 라인 수는 합산한다.
        author 정보는 첫 번째 diff 기준으로 사용한다.
        """
        return GitDiff(
            commit_hash=",".join(d["commit_hash"] for d in diffs),
            prev_hash=",".join(d["prev_hash"] for d in diffs if d["prev_hash"]),
            changed_files=[f for d in diffs for f in d["changed_files"]],
            added_lines=sum(d["added_lines"] for d in diffs),
            deleted_lines=sum(d["deleted_lines"] for d in diffs),
            diff_detail=[item for d in diffs for item in d["diff_detail"]],
            author=diffs[0]["author"],
            author_email=diffs[0]["author_email"],
            commit_timestamp=diffs[0]["commit_timestamp"],
            blame=[item for d in diffs for item in d["blame"]],
        )

    # ── 라우터 prefix 처리 ────────────────────────────────────────────────────

    async def _build_router_prefix_map(
        self, adapter: GitPlatformAdapter, branch: str, all_paths: list[str]
    ) -> dict[str, str]:
        """main.py / app.py의 include_router 호출에서 모듈별 prefix를 추출한다.

        Returns:
            {module_stem: prefix} — 예: {"auth": "/api/auth", "orders": "/api/orders"}
        """
        name_to_paths: dict[str, list[str]] = {}
        for p in all_paths:
            name_to_paths.setdefault(PurePosixPath(p).name, []).append(p)

        prefix_map: dict[str, str] = {}
        for entry in ("main.py", "app.py"):
            for full_path in name_to_paths.get(entry, []):
                try:
                    content = await adapter.get_file_content(full_path, branch)
                    parser = self._get_parser("python")
                    if parser is None:
                        continue
                    tree = parser.parse(content)

                    # import alias 역추적: "from X.Y import Z as W" → alias_map[W] = Y
                    alias_map: dict[str, str] = {}
                    for imp in _find_nodes(tree.root_node, "import_from_statement"):
                        parent_mod = _node_text(imp.child_by_field_name("module_name") or imp.children[1])
                        parent_stem = PurePosixPath(parent_mod.replace(".", "/")).name
                        for child in imp.named_children:
                            if child.type == "aliased_import":
                                orig = _node_text(child.child_by_field_name("name"))
                                alias = _node_text(child.child_by_field_name("alias"))
                                alias_map[alias] = parent_stem
                                alias_map[orig] = parent_stem
                            elif child.type == "dotted_name":
                                alias_map[_node_text(child)] = parent_stem

                    for call_node in _find_nodes(tree.root_node, "call"):
                        func_node = call_node.child_by_field_name("function")
                        if not func_node or func_node.type != "attribute":
                            continue
                        if _node_text(func_node.child_by_field_name("attribute")) != "include_router":
                            continue
                        args = call_node.child_by_field_name("arguments")
                        if not args:
                            continue
                        positional = [c for c in args.named_children if c.type != "keyword_argument"]
                        if not positional:
                            continue
                        # "auth.router" → "auth", plain alias → alias_map 역추적
                        router_ref = _node_text(positional[0])
                        module_stem = router_ref.split(".")[0]
                        if "." not in router_ref:
                            module_stem = alias_map.get(module_stem, module_stem)
                        prefix = ""
                        for kw in args.named_children:
                            if kw.type == "keyword_argument":
                                if _node_text(kw.child_by_field_name("name")) == "prefix":
                                    prefix = _node_text(kw.child_by_field_name("value")).strip("\"'")
                        if prefix and module_stem:
                            prefix_map[module_stem] = prefix
                except Exception as e:
                    self.logger.warning("router_prefix_parse_failed", path=full_path, error=str(e))
        return prefix_map

    @staticmethod
    def _apply_router_prefixes(file_infos: list[FileInfo], prefix_map: dict[str, str]) -> None:
        """파일 stem 기반으로 endpoint path에 라우터 prefix를 적용한다."""
        for fi in file_infos:
            stem = PurePosixPath(fi["path"]).stem
            prefix = prefix_map.get(stem, "")
            if not prefix:
                continue
            for ep in fi["endpoints"]:
                sub_path = ep["path"]
                ep["path"] = prefix + sub_path if sub_path else prefix

    # ── 스킵 체크 ─────────────────────────────────────────────────────────────

    async def _check_skip(
        self,
        adapter: GitPlatformAdapter,
        trigger: str,
        branch: str,
        last_commit_hash: str,
    ) -> tuple[dict | None, str]:
        """트리거 조건에 따라 스캔 스킵 여부를 결정한다.

        Returns:
            스킵 시 (결과 dict, ""), 계속 시 (None, 유효 트리거).
        """
        if trigger == "doc_update":
            return (
                {"scan_result": None, "_metadata": {"skipped": True, "reason": "doc_update"}},
                "",
            )
        if trigger == "natural_lang":
            if last_commit_hash:
                head_hash = await adapter.get_head_commit_hash(branch)
                if last_commit_hash == head_hash:
                    return (
                        {"scan_result": None, "_metadata": {"skipped": True, "reason": "no_code_change"}},
                        "",
                    )
            return None, "code_change"
        return None, trigger

    # ── 파일 수집 ─────────────────────────────────────────────────────────────

    async def _collect_files(
        self,
        adapter: GitPlatformAdapter,
        trigger: str,
        branch: str,
        last_commit_hash: str,
        head_hash: str,
    ) -> list[str]:
        """스캔 대상 파일 경로 목록을 수집한다.

        code_change 트리거이고 last_commit_hash가 있으면 diff 기반 증분 수집,
        그 외에는 전체 파일 목록을 반환한다.
        """
        if trigger == "code_change" and last_commit_hash:
            try:
                diff = await adapter.get_diff(last_commit_hash, head_hash)
                changed = [d["file"] for d in diff if self._should_scan_path(d["file"])]
                if changed:
                    return changed
            except ToolExecutionError:
                pass
        raw = await adapter.get_file_list(branch)
        return [f["path"] for f in raw if self._should_scan_path(f["path"])]

    def _should_scan_path(self, path_str: str) -> bool:
        """스캔 대상 파일 여부를 반환한다."""
        p = PurePosixPath(path_str)
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            return False
        if any(fnmatch.fnmatch(p.name, pat) for pat in _EXCLUDE_PATTERNS):
            return False
        return p.suffix.lower() in _LANG_MAP or p.name in _CONFIG_FILES

    # ── 언어/프레임워크 감지 ──────────────────────────────────────────────────

    async def _detect_language_framework(
        self, adapter: GitPlatformAdapter, branch: str, all_paths: list[str]
    ) -> tuple[str, str]:
        """주 언어와 프레임워크를 감지한다."""
        names = {PurePosixPath(p).name for p in all_paths}
        if "pom.xml" in names:
            return "java", "spring"
        if "requirements.txt" in names or "pyproject.toml" in names:
            framework = await self._detect_python_framework(adapter, branch, all_paths)
            return "python", framework
        if "package.json" in names:
            lang = "typescript" if "tsconfig.json" in names else "javascript"
            framework = await self._detect_node_framework(adapter, branch)
            return lang, framework
        counts: dict[str, int] = {}
        for path in all_paths:
            lang = _LANG_MAP.get(PurePosixPath(path).suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        top = max(counts, key=lambda k: counts[k]) if counts else "unknown"
        return top, "unknown"

    async def _detect_python_framework(
        self, adapter: GitPlatformAdapter, branch: str, all_paths: list[str]
    ) -> str:
        """Python 프레임워크를 감지한다."""
        name_to_path = {PurePosixPath(p).name: p for p in all_paths}
        for filename in ("main.py", "app.py"):
            full_path = name_to_path.get(filename)
            if full_path is None:
                continue
            try:
                content = await adapter.get_file_content(full_path, branch)
                text = content.decode("utf-8", errors="replace").lower()
                if "fastapi" in text:
                    return "fastapi"
                if "django" in text:
                    return "django"
                if "flask" in text:
                    return "flask"
            except ToolExecutionError:
                pass
        return "unknown"

    async def _detect_node_framework(
        self, adapter: GitPlatformAdapter, branch: str
    ) -> str:
        """Node.js 프레임워크를 감지한다."""
        try:
            content = await adapter.get_file_content("package.json", branch)
            data = json.loads(content.decode("utf-8", errors="replace"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "express" in deps:
                return "express"
        except Exception:
            pass
        return "unknown"

    # ── 파일 파싱 ─────────────────────────────────────────────────────────────

    def _parse_file_from_content(
        self,
        file_path_str: str,
        language: str,
        content: bytes,
    ) -> FileInfo:
        """API에서 받은 파일 내용을 파싱하여 FileInfo를 반환한다.

        Args:
            file_path_str: 경로 판별용 문자열 (파일 읽기에 사용하지 않음).
            language: 파일 언어.
            content: API에서 받은 파일 내용.

        Returns:
            FileInfo.
        """
        try:
            p = PurePosixPath(file_path_str)
            if p.name in (".env", ".env.example"):
                return FileInfo(
                    path=file_path_str, language="env",
                    endpoints=[], functions=[],
                    dependencies=self._parse_env_content(content),
                    models=[],
                )
            if p.suffix == ".properties":
                return FileInfo(
                    path=file_path_str, language="properties",
                    endpoints=[], functions=[],
                    dependencies=self._parse_properties_content(content),
                    models=[],
                )
            parser = self._get_parser(language)
            if parser is None:
                self.logger.warning(
                    "unsupported_language", path=file_path_str, language=language
                )
                return FileInfo(
                    path=file_path_str, language=language,
                    endpoints=[], functions=[], dependencies=[], models=[],
                )
            tree = parser.parse(content)
            endpoints, functions, deps = self._dispatch_parse(file_path_str, language, tree)
            models = self._dispatch_extract_models(language, tree)
            for ep in endpoints:
                ep["file"] = file_path_str
            src_lines = content.decode("utf-8", errors="replace").splitlines()
            for fn in functions:
                fn["file"] = file_path_str
                line_start = fn.get("line_start", 0)
                line_end = fn.get("line_end", line_start)
                if line_start:
                    excerpt = src_lines[line_start - 1 : min(line_end, line_start + 40) - 1]
                    fn["body_excerpt"] = "\n".join(excerpt)
            return FileInfo(
                path=file_path_str, language=language,
                endpoints=endpoints, functions=functions,
                dependencies=deps, models=models,
            )
        except Exception as e:
            self.logger.warning("parse_failed", path=file_path_str, error=str(e))
            return FileInfo(
                path=file_path_str, language=language,
                endpoints=[], functions=[], dependencies=[], models=[],
            )

    def _parse_env_content(self, content: bytes) -> list[str]:
        """`.env` 내용을 파싱하여 마스킹된 key=value 목록을 반환한다."""
        result: list[str] = []
        for line in content.decode("utf-8", errors="replace").splitlines():
            m = _ENV_RE.match(line.strip())
            if m:
                key, val = m.group(1), m.group(2)
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    def _parse_properties_content(self, content: bytes) -> list[str]:
        """`.properties` 내용을 파싱하여 마스킹된 key=value 목록을 반환한다."""
        result: list[str] = []
        for line in content.decode("utf-8", errors="replace").splitlines():
            m = _PROP_RE.match(line)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    # ── Git 이력 ──────────────────────────────────────────────────────────────

    async def _extract_git_diff(
        self,
        adapter: GitPlatformAdapter,
        branch: str,
        last_commit_hash: str,
    ) -> GitDiff | None:
        """어댑터 API 호출로 Git 이력을 추출하여 GitDiff를 반환한다.

        Args:
            adapter: Git 플랫폼 어댑터.
            branch: 대상 브랜치.
            last_commit_hash: 이전 commit hash.
                제공되면 last_commit_hash..HEAD 증분 diff,
                없으면 HEAD~1..HEAD (가장 최근 커밋의 변경 내역)를 사용한다.

        Returns:
            GitDiff 또는 None (오류 시).
        """
        try:
            head_hash = await adapter.get_head_commit_hash(branch)
            commits = await adapter.get_commits(branch)
            if not commits:
                return None
            latest = commits[0]
            author = latest.get("author", "")
            author_email = latest.get("author_email", "")
            commit_timestamp = latest.get("timestamp", "")

            # last_commit_hash 없으면 HEAD~1을 기준으로 사용
            parent_hash = (
                last_commit_hash
                if last_commit_hash and last_commit_hash != head_hash
                else (commits[1]["hash"] if len(commits) > 1 else "")
            )

            diff_detail: list[dict] = []
            changed_files: list[str] = []
            added_lines = 0
            deleted_lines = 0

            if parent_hash:
                diff_detail = await adapter.get_diff(parent_hash, head_hash)
                changed_files = [d["file"] for d in diff_detail]
                added_lines = sum(d["added"] for d in diff_detail)
                deleted_lines = sum(d["deleted"] for d in diff_detail)

            blame: list[dict] = []
            if changed_files:
                if isinstance(adapter, GitHubAdapter):
                    self.logger.warning("blame_unsupported", platform="github")
                else:
                    for file_path in changed_files:
                        try:
                            entries = await adapter.get_blame(file_path, branch)
                            blame.extend(entries)
                        except ToolExecutionError as e:
                            self.logger.warning(
                                "blame_failed", file=file_path, error=str(e)
                            )

            return GitDiff(
                commit_hash=head_hash,
                prev_hash=parent_hash,
                changed_files=changed_files,
                added_lines=added_lines,
                deleted_lines=deleted_lines,
                diff_detail=diff_detail,
                author=author,
                author_email=author_email,
                commit_timestamp=commit_timestamp,
                blame=blame,
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.warning("git_diff_failed", error=str(e))
            return None

    # ── 파서 초기화 & 디스패치 ─────────────────────────────────────────────────

    def _get_parser(self, language: str) -> Parser | None:
        """언어별 tree-sitter 파서를 반환한다 (lazy init)."""
        if language in self._parsers:
            return self._parsers[language]
        try:
            if language == "python":
                lang = Language(tspython.language())
            elif language in ("typescript", "tsx"):
                lang = Language(tsts.language_typescript())
            elif language == "javascript":
                lang = Language(tsjs.language())
            elif language == "java":
                lang = Language(tsjava.language())
            elif language == "yaml":
                lang = Language(tsyaml.language())
            else:
                return None
            parser = Parser(lang)
            self._parsers[language] = parser
            return parser
        except Exception as e:
            self.logger.warning("parser_init_failed", language=language, error=str(e))
            return None

    def _dispatch_parse(
        self, file_path_str: str, language: str, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """언어별 파서를 호출한다."""
        if language == "python":
            return self._parse_python(tree)
        if language in ("typescript", "javascript"):
            return self._parse_typescript(file_path_str, tree)
        if language == "java":
            return self._parse_java(tree)
        if language == "yaml":
            return [], [], self._parse_yaml_config(tree)
        return [], [], []

    def _dispatch_extract_models(self, language: str, tree: Any) -> list[dict]:
        """언어별 데이터 모델 추출기를 호출한다."""
        if language == "python":
            return self._extract_models_python(tree)
        if language in ("typescript", "javascript"):
            return self._extract_models_typescript(tree)
        if language == "java":
            return self._extract_models_java(tree)
        return []

    # ── Python 파싱 ──────────────────────────────────────────────────────────

    def _parse_python(
        self, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Python 파일을 파싱하여 엔드포인트, 함수, 의존성을 추출한다."""
        root = tree.root_node
        endpoints: list[dict] = []
        functions: list[dict] = []
        deps: list[str] = []

        for node in _find_nodes(root, "import_statement"):
            deps.append(_node_text(node).strip())
        for node in _find_nodes(root, "import_from_statement"):
            deps.append(_node_text(node).strip())

        for node in _find_nodes(root, "decorated_definition"):
            ep = self._extract_py_endpoint(node)
            if ep:
                endpoints.append(ep)
            defn = node.child_by_field_name("definition")
            if defn and defn.type == "function_definition":
                fn = self._extract_py_function(defn)
                if fn:
                    fn["calls"] = self._extract_calls_from_tree(defn, "python")
                    functions.append(fn)

        for node in root.named_children:
            if node.type == "function_definition":
                fn = self._extract_py_function(node)
                if fn:
                    fn["calls"] = self._extract_calls_from_tree(node, "python")
                    functions.append(fn)

        return endpoints, functions, deps

    def _extract_py_endpoint(self, node: Node) -> dict | None:
        """Python decorated_definition에서 엔드포인트 정보를 추출한다."""
        for dec in _find_nodes(node, "decorator"):
            call = next((c for c in dec.children if c.type == "call"), None)
            if not call:
                continue
            func = call.child_by_field_name("function")
            if not func or func.type != "attribute":
                continue
            method = _node_text(func.child_by_field_name("attribute")).lower()
            if method not in _HTTP_ROUTE_ATTRS:
                continue
            args = call.child_by_field_name("arguments")
            path = ""
            response_model = ""
            if args:
                positional = [
                    c for c in args.named_children
                    if c.type != "keyword_argument"
                ]
                if positional:
                    path = _node_text(positional[0]).strip("\"'")
                for kw in args.named_children:
                    if kw.type == "keyword_argument":
                        if _node_text(kw.child_by_field_name("name")) == "response_model":
                            response_model = _node_text(kw.child_by_field_name("value"))
            defn = node.child_by_field_name("definition")
            handler = _node_text(defn.child_by_field_name("name")) if defn else ""
            params = _params_to_list(defn.child_by_field_name("parameters") if defn else None)
            return {
                "method": method.upper(),
                "path": path,
                "handler": handler,
                "params": params,
                "response_model": response_model,
            }
        return None

    @staticmethod
    def _extract_py_function(node: Node | None) -> dict | None:
        """Python function_definition 노드에서 함수 정보를 추출한다."""
        if not node or node.type != "function_definition":
            return None
        return {
            "name": _node_text(node.child_by_field_name("name")),
            "line_start": node.start_point[0] + 1,
            "line_end": node.end_point[0] + 1,
            "params": _params_to_list(node.child_by_field_name("parameters")),
            "return_type": _node_text(node.child_by_field_name("return_type")),
        }

    # ── TypeScript / JavaScript 파싱 ──────────────────────────────────────────

    def _parse_typescript(
        self, file_path_str: str, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """TypeScript/JavaScript 파일을 파싱한다."""
        root = tree.root_node
        endpoints: list[dict] = []
        functions: list[dict] = []
        deps: list[str] = []

        for node in _find_nodes(root, "import_statement"):
            deps.append(_node_text(node).strip())

        for node in _find_nodes(root, "call_expression"):
            ep = self._extract_express_endpoint(node)
            if ep:
                endpoints.append(ep)

        path_str = file_path_str.replace("\\", "/")
        if "pages/api/" in path_str or "app/api/" in path_str:
            endpoints.extend(self._extract_nextjs_endpoints(file_path_str))

        for node in _find_nodes(root, "function_declaration"):
            fn = self._extract_ts_function(node)
            if fn:
                fn["calls"] = self._extract_calls_from_tree(node, "typescript")
                functions.append(fn)
        for node in _find_nodes(root, "arrow_function"):
            fn = self._extract_ts_arrow_function(node)
            if fn:
                fn["calls"] = self._extract_calls_from_tree(node, "typescript")
                functions.append(fn)

        return endpoints, functions, deps

    @staticmethod
    def _extract_express_endpoint(node: Node) -> dict | None:
        """Express call_expression에서 엔드포인트를 추출한다."""
        func = node.child_by_field_name("function")
        if not func or func.type != "member_expression":
            return None
        method = _node_text(func.child_by_field_name("property")).lower()
        if method not in _HTTP_VERBS:
            return None
        args = node.child_by_field_name("arguments")
        if not args or not args.named_children:
            return None
        path = _node_text(args.named_children[0]).strip("\"'`")
        return {
            "method": method.upper(),
            "path": path,
            "handler": "",
            "params": [],
            "response_model": "",
        }

    @staticmethod
    def _extract_nextjs_endpoints(file_path_str: str) -> list[dict]:
        """Next.js 파일 경로 기반으로 엔드포인트를 생성한다."""
        path_str = file_path_str.replace("\\", "/")
        for marker in ("pages/api/", "app/api/"):
            if marker in path_str:
                api_part = path_str.split(marker, 1)[1]
                route = "/" + api_part.rsplit(".", 1)[0]
                route = route.replace("/index", "").replace("[", ":").replace("]", "")
                return [{
                    "method": "ANY",
                    "path": route,
                    "handler": PurePosixPath(file_path_str).stem,
                    "params": [],
                    "response_model": "",
                }]
        return []

    @staticmethod
    def _extract_ts_function(node: Node) -> dict | None:
        """TypeScript function_declaration 노드에서 함수 정보를 추출한다."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        return {
            "name": _node_text(name_node),
            "line_start": node.start_point[0] + 1,
            "line_end": node.end_point[0] + 1,
            "params": _params_to_list(node.child_by_field_name("parameters")),
            "return_type": _node_text(node.child_by_field_name("return_type")),
        }

    @staticmethod
    def _extract_ts_arrow_function(node: Node) -> dict | None:
        """변수 선언에 바인딩된 arrow_function에서 함수 정보를 추출한다."""
        parent = node.parent
        if not parent or parent.type != "variable_declarator":
            return None
        params_node = (
            node.child_by_field_name("parameters")
            or node.child_by_field_name("parameter")
        )
        return {
            "name": _node_text(parent.child_by_field_name("name")),
            "line_start": node.start_point[0] + 1,
            "line_end": node.end_point[0] + 1,
            "params": _params_to_list(params_node),
            "return_type": "",
        }

    # ── Java 파싱 ─────────────────────────────────────────────────────────────

    def _parse_java(self, tree: Any) -> tuple[list[dict], list[dict], list[str]]:
        """Java 파일을 파싱하여 엔드포인트, 함수, 의존성을 추출한다."""
        root = tree.root_node
        endpoints: list[dict] = []
        functions: list[dict] = []
        deps: list[str] = []

        for node in _find_nodes(root, "import_declaration"):
            deps.append(_node_text(node).strip())

        for method_node in _find_nodes(root, "method_declaration"):
            fn = self._extract_java_function(method_node)
            if fn:
                fn["calls"] = self._extract_calls_from_tree(method_node, "java")
                functions.append(fn)
            ep = self._extract_java_endpoint(method_node)
            if ep:
                endpoints.append(ep)

        return endpoints, functions, deps

    def _extract_java_endpoint(self, method_node: Node) -> dict | None:
        """Java method_declaration에서 Spring MVC 엔드포인트를 추출한다."""
        handler = _node_text(method_node.child_by_field_name("name"))
        params = _params_to_list(method_node.child_by_field_name("formal_parameters"))
        for ann in _find_nodes(method_node, "marker_annotation"):
            ann_name = _node_text(ann.child_by_field_name("name"))
            if ann_name in _JAVA_MAPPINGS:
                return {
                    "method": _JAVA_MAPPINGS[ann_name] or "ANY",
                    "path": "",
                    "handler": handler,
                    "params": params,
                    "response_model": "",
                }
        for ann in _find_nodes(method_node, "annotation"):
            ann_name = _node_text(ann.child_by_field_name("name"))
            if ann_name in _JAVA_MAPPINGS:
                return {
                    "method": _JAVA_MAPPINGS[ann_name] or "ANY",
                    "path": self._extract_java_annotation_value(ann),
                    "handler": handler,
                    "params": params,
                    "response_model": "",
                }
        return None

    @staticmethod
    def _extract_java_annotation_value(ann_node: Node) -> str:
        """Java 어노테이션에서 value 또는 path 속성 값을 추출한다."""
        for child in ann_node.named_children:
            if child.type == "element_value_pair":
                key = _node_text(child.child_by_field_name("key"))
                if key in ("value", "path"):
                    return _node_text(child.child_by_field_name("value")).strip('"')
            elif child.type == "string_literal":
                return _node_text(child).strip('"')
        return ""

    @staticmethod
    def _extract_java_function(node: Node) -> dict | None:
        """Java method_declaration 노드에서 함수 정보를 추출한다."""
        name_node = node.child_by_field_name("name")
        if not name_node:
            return None
        return {
            "name": _node_text(name_node),
            "line_start": node.start_point[0] + 1,
            "line_end": node.end_point[0] + 1,
            "params": _params_to_list(node.child_by_field_name("formal_parameters")),
            "return_type": _node_text(node.child_by_field_name("type")),
        }

    # ── 함수 호출 추출 ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_calls_from_tree(node: Node, language: str) -> list[str]:
        """함수 노드에서 호출되는 함수 이름 목록을 추출한다."""
        calls: list[str] = []
        if language == "python":
            for call_node in _find_nodes(node, "call"):
                func_node = call_node.child_by_field_name("function")
                if func_node:
                    name = _node_text(
                        func_node.child_by_field_name("attribute") or func_node
                    )
                    if name and name not in calls:
                        calls.append(name)
        elif language in ("typescript", "javascript"):
            for call_node in _find_nodes(node, "call_expression"):
                func_node = call_node.child_by_field_name("function")
                if func_node:
                    name = _node_text(
                        func_node.child_by_field_name("property") or func_node
                    )
                    if name and name not in calls:
                        calls.append(name)
        elif language == "java":
            for call_node in _find_nodes(node, "method_invocation"):
                name_node = call_node.child_by_field_name("name")
                if name_node:
                    name = _node_text(name_node)
                    if name and name not in calls:
                        calls.append(name)
        return calls

    # ── 데이터 모델 추출 ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_models_python(tree: Any) -> list[dict]:
        """Python BaseModel / TypedDict / dataclass 클래스를 추출한다."""
        models: list[dict] = []
        for node in tree.root_node.named_children:
            target, has_dataclass = node, False
            if node.type == "decorated_definition":
                has_dataclass = any(
                    "dataclass" in _node_text(d)
                    for d in _find_nodes(node, "decorator")
                )
                inner = node.child_by_field_name("definition")
                if not inner or inner.type != "class_definition":
                    continue
                target = inner
            elif node.type != "class_definition":
                continue
            bases = _node_text(target.child_by_field_name("superclasses"))
            if not has_dataclass and not any(b in bases for b in ("BaseModel", "TypedDict")):
                continue
            name = _node_text(target.child_by_field_name("name"))
            fields: list[str] = []
            body = target.child_by_field_name("body")
            if body:
                for stmt in body.named_children:
                    if stmt.type == "annotated_assignment":
                        f = _node_text(stmt.child_by_field_name("left"))
                        if f:
                            fields.append(f)
                    elif stmt.type == "expression_statement":
                        for child in stmt.named_children:
                            if child.type == "assignment":
                                f = _node_text(child.child_by_field_name("left"))
                                if f and f not in fields:
                                    fields.append(f)
                            elif child.type == "typed_parameter":
                                f = _node_text(child.child_by_field_name("name"))
                                if f and f not in fields:
                                    fields.append(f)
                            elif child.type == "identifier" and ":" in _node_text(stmt):
                                f = _node_text(child)
                                if f and f not in fields:
                                    fields.append(f)
            models.append({"name": name, "fields": fields, "source": "python"})
        return models

    @staticmethod
    def _extract_models_typescript(tree: Any) -> list[dict]:
        """TypeScript interface / type alias 선언에서 모델을 추출한다."""
        models: list[dict] = []
        root = tree.root_node
        for node in _find_nodes(root, "interface_declaration"):
            name = _node_text(node.child_by_field_name("name"))
            fields: list[str] = []
            body = node.child_by_field_name("body")
            if body:
                for member in body.named_children:
                    if member.type == "property_signature":
                        f = _node_text(member.child_by_field_name("name"))
                        if f:
                            fields.append(f)
            models.append({"name": name, "fields": fields, "source": "typescript"})
        for node in _find_nodes(root, "type_alias_declaration"):
            name = _node_text(node.child_by_field_name("name"))
            value = node.child_by_field_name("value")
            if not value or value.type != "object_type":
                continue
            fields = [
                _node_text(m.child_by_field_name("name"))
                for m in value.named_children
                if m.type == "property_signature"
                and _node_text(m.child_by_field_name("name"))
            ]
            models.append({"name": name, "fields": fields, "source": "typescript"})
        return models

    @staticmethod
    def _extract_models_java(tree: Any) -> list[dict]:
        """Java @Entity / @Data / @Table 클래스에서 모델을 추출한다."""
        models: list[dict] = []
        for node in _find_nodes(tree.root_node, "class_declaration"):
            all_anns = {
                _node_text(a.child_by_field_name("name"))
                for a in _find_nodes(node, "marker_annotation")
            } | {
                _node_text(a.child_by_field_name("name"))
                for a in _find_nodes(node, "annotation")
            }
            if not all_anns & _MODEL_ANNS:
                continue
            name = _node_text(node.child_by_field_name("name"))
            fields: list[str] = []
            body = node.child_by_field_name("body")
            if body:
                for decl in _find_nodes(body, "field_declaration"):
                    for d in _find_nodes(decl, "variable_declarator"):
                        f = _node_text(d.child_by_field_name("name"))
                        if f:
                            fields.append(f)
            models.append({"name": name, "fields": fields, "source": "java"})
        return models

    # ── 설정 파일 파싱 ────────────────────────────────────────────────────────

    def _parse_yaml_config(self, tree: Any) -> list[str]:
        """YAML 설정 파일을 파싱하여 마스킹된 key=value 목록을 반환한다."""
        result: list[str] = []
        for pair in _find_nodes(tree.root_node, "block_mapping_pair"):
            key = _node_text(pair.child_by_field_name("key")).strip()
            val_node = pair.child_by_field_name("value")
            val = _node_text(val_node).strip() if val_node else ""
            if key:
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    @staticmethod
    def _mask_sensitive(key: str, value: str) -> str:
        """민감 키의 값을 마스킹한다."""
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            return "***"
        return value

    @staticmethod
    def _parse_stat(stat_output: str) -> tuple[int, int]:
        """git diff --stat 출력에서 추가/삭제 라인 수를 추출한다."""
        m = _STAT_RE.search(stat_output)
        if m:
            return int(m.group(1)), int(m.group(2))
        added = deleted = 0
        ins = re.search(r"(\d+) insertions?\(\+\)", stat_output)
        del_ = re.search(r"(\d+) deletions?\(-\)", stat_output)
        if ins:
            added = int(ins.group(1))
        if del_:
            deleted = int(del_.group(1))
        return added, deleted
