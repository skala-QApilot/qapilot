"""코드베이스 스캔 Tool.

프로젝트 소스코드를 정적 분석하여 API 엔드포인트, 데이터 모델,
프레임워크, Git 이력을 추출하고 로컬 manifest.json에 캐싱한다.

담당: C
Created: 2026-05-07
"""

import fnmatch
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import git
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
    ".qapilot",       # QApilot 자체 산출물 제외 (시나리오, 생성 코드 등)
    ".pytest_cache",
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


def _is_excluded(path: Path) -> bool:
    """파일이 제외 패턴에 해당하는지 확인한다."""
    return any(fnmatch.fnmatch(path.name, pat) for pat in _EXCLUDE_PATTERNS)


# ── Tool ───────────────────────────────────────────────────────────────────────

class CodebaseScannerTool(BaseTool):
    """코드베이스 스캔 Tool.

    역할: AST 파싱, 엔드포인트/함수/종속성 추출, 증분 재분석
    입력: params["trigger"] = "init" | "code_change"
    출력: {"scan_result": ScanResult}
    저장: .qapilot/manifest.json (로컬 캐시)
    스캔 결과는 서버 DB 업로드 (pipeline.py 노드 담당)
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._parsers: dict[str, Parser] = {}

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """코드베이스를 스캔하고 ScanResult를 반환한다.

        Args:
            params: {"trigger": "init" | "code_change"}

        Returns:
            {"scan_result": ScanResult}

        Raises:
            ToolExecutionError: repo_path 미존재 시.
        """
        trigger = params.get("trigger", "init")
        repo_path = self._get_repo_path()
        skip_result, trigger = self._check_skip(trigger, repo_path)
        if skip_result is not None:
            return skip_result
        manifest = self._load_manifest(repo_path)
        files = self._collect_files(repo_path, trigger, manifest)
        language, framework = self._detect_language_framework(repo_path)

        file_infos: list[FileInfo] = []
        for f in files:
            file_lang = _LANG_MAP.get(f.suffix.lower(), "unknown")
            fi = self._parse_file(f, file_lang)
            file_infos.append(fi)

        git_diff = self._extract_git_diff(repo_path)
        commit_hash = git_diff["commit_hash"] if git_diff else ""
        self._save_manifest(repo_path, commit_hash, [fi["path"] for fi in file_infos])

        scan_result: ScanResult = {
            "files": file_infos,
            "git_diff": git_diff,
            "framework": framework,
            "language": language,
            "endpoint_count": sum(len(fi["endpoints"]) for fi in file_infos),
        }
        self.logger.info(
            "scan_complete",
            files=len(file_infos),
            endpoints=scan_result["endpoint_count"],
            framework=framework,
        )
        return {
            "scan_result": scan_result,
            "_metadata": {"trigger": trigger, "files_scanned": len(file_infos)},
        }

    # ── 설정 & 경로 ───────────────────────────────────────────────────────────

    def _get_repo_path(self) -> Path:
        """설정에서 repo_path를 읽는다. 없으면 CWD를 사용한다.

        Returns:
            스캔 대상 저장소 경로.

        Raises:
            ToolExecutionError: 경로가 존재하지 않을 경우.
        """
        proj = self._config.project
        raw = proj.root or proj.repo_path
        path = Path(raw) if raw else Path(".")
        if not path.exists():
            raise ToolExecutionError(ErrorCode.TOOL_001, f"repo_path 미존재: {path}")
        return path.resolve()

    # ── 언어/프레임워크 감지 ──────────────────────────────────────────────────

    def _detect_language_framework(self, repo_path: Path) -> tuple[str, str]:
        """주 언어와 프레임워크를 감지한다.

        Args:
            repo_path: 저장소 루트 경로.

        Returns:
            (language, framework) 튜플.
        """
        if (repo_path / "pom.xml").exists():
            return "java", "spring"
        if (repo_path / "requirements.txt").exists() or (repo_path / "pyproject.toml").exists():
            return "python", self._detect_python_framework(repo_path)
        if (repo_path / "package.json").exists():
            lang = "typescript" if (repo_path / "tsconfig.json").exists() else "javascript"
            return lang, self._detect_node_framework(repo_path)
        counts: dict[str, int] = {}
        for p in repo_path.rglob("*"):
            if p.is_file():
                lang = _LANG_MAP.get(p.suffix.lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
        top = max(counts, key=lambda k: counts[k]) if counts else "unknown"
        return top, "unknown"

    def _detect_python_framework(self, repo_path: Path) -> str:
        """Python 프레임워크를 감지한다."""
        for name in ("main.py", "app.py"):
            target = repo_path / name
            if target.exists():
                text = target.read_text(encoding="utf-8", errors="replace").lower()
                if "fastapi" in text:
                    return "fastapi"
                if "django" in text:
                    return "django"
                if "flask" in text:
                    return "flask"
        return "unknown"

    def _detect_node_framework(self, repo_path: Path) -> str:
        """Node.js 프레임워크를 감지한다."""
        try:
            data = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps:
                return "nextjs"
            if "express" in deps:
                return "express"
        except Exception:
            pass
        return "unknown"

    # ── 파일 수집 ─────────────────────────────────────────────────────────────

    def _collect_files(
        self, repo_path: Path, trigger: str, manifest: dict | None
    ) -> list[Path]:
        """스캔 대상 파일 목록을 수집한다.

        Args:
            repo_path: 저장소 루트 경로.
            trigger: "init" | "code_change".
            manifest: 이전 스캔 manifest. None이면 풀 스캔.

        Returns:
            스캔 대상 파일 경로 목록.
        """
        if trigger == "code_change" and manifest:
            return self._collect_changed_files(repo_path, manifest)
        return self._collect_all_files(repo_path)

    def _collect_all_files(self, repo_path: Path) -> list[Path]:
        """저장소 전체에서 스캔 대상 파일을 수집한다."""
        result: list[Path] = []
        for path in repo_path.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.relative_to(repo_path).parts):
                continue
            if _is_excluded(path):
                continue
            if path.suffix.lower() in _LANG_MAP or path.name in _CONFIG_FILES:
                result.append(path)
        return result

    def _collect_changed_files(self, repo_path: Path, manifest: dict) -> list[Path]:
        """Git diff 기반으로 변경된 파일만 수집한다."""
        last_hash = manifest.get("last_commit_hash", "")
        if not last_hash:
            return self._collect_all_files(repo_path)
        try:
            repo = git.Repo(repo_path)
            repo.commit(last_hash)
            changed = repo.git.diff(f"{last_hash}..HEAD", "--name-only")
            paths = [
                repo_path / p
                for p in changed.splitlines()
                if (repo_path / p).exists()
                and not _is_excluded(repo_path / p)
                and (
                    (repo_path / p).suffix.lower() in _LANG_MAP
                    or (repo_path / p).name in _CONFIG_FILES
                )
            ]
            return paths if paths else self._collect_all_files(repo_path)
        except Exception:
            return self._collect_all_files(repo_path)

    # ── 파일 파싱 ─────────────────────────────────────────────────────────────

    def _parse_file(self, file_path: Path, language: str) -> FileInfo:
        """단일 파일을 파싱하여 FileInfo를 반환한다.

        Args:
            file_path: 파싱할 파일 경로.
            language: 파일 언어 (_LANG_MAP 기준).

        Returns:
            FileInfo.
        """
        try:
            if file_path.name in (".env", ".env.example"):
                return FileInfo(
                    path=str(file_path), language="env",
                    endpoints=[], functions=[],
                    dependencies=self._parse_env_file(file_path),
                    models=[],
                )
            if file_path.suffix == ".properties":
                return FileInfo(
                    path=str(file_path), language="properties",
                    endpoints=[], functions=[],
                    dependencies=self._parse_properties_file(file_path),
                    models=[],
                )
            parser = self._get_parser(language)
            if parser is None:
                self.logger.warning("unsupported_language", path=str(file_path), language=language)
                return FileInfo(path=str(file_path), language=language,
                                endpoints=[], functions=[], dependencies=[], models=[])
            tree = parser.parse(file_path.read_bytes())
            endpoints, functions, deps = self._dispatch_parse(file_path, language, tree)
            models = self._dispatch_extract_models(file_path, language, tree)
            return FileInfo(path=str(file_path), language=language,
                            endpoints=endpoints, functions=functions, dependencies=deps,
                            models=models)
        except Exception as e:
            self.logger.warning("parse_failed", path=str(file_path), error=str(e))
            return FileInfo(path=str(file_path), language=language,
                            endpoints=[], functions=[], dependencies=[], models=[])

    def _dispatch_parse(
        self, file_path: Path, language: str, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """언어별 파서를 호출한다."""
        if language == "python":
            return self._parse_python(tree)
        if language in ("typescript", "javascript"):
            return self._parse_typescript(file_path, tree)
        if language == "java":
            return self._parse_java(file_path, tree)
        if language == "yaml":
            return [], [], self._parse_yaml_config(tree)
        return [], [], []

    def _get_parser(self, language: str) -> Parser | None:
        """언어별 tree-sitter 파서를 반환한다 (lazy init).

        Args:
            language: 언어 이름.

        Returns:
            Parser 또는 None (미지원 언어).
        """
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

    # ── Python 파싱 ──────────────────────────────────────────────────────────

    def _parse_python(
        self, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Python 파일을 파싱하여 엔드포인트, 함수, 의존성을 추출한다.

        Args:
            tree: tree-sitter 파싱 결과.

        Returns:
            (endpoints, functions, dependencies) 튜플.
        """
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
            if args and args.named_children:
                path = _node_text(args.named_children[0]).strip("\"'")
            defn = node.child_by_field_name("definition")
            handler = _node_text(defn.child_by_field_name("name")) if defn else ""
            requires_auth = False
            if defn:
                params_text = _node_text(defn.child_by_field_name("parameters"))
                requires_auth = "get_current_customer" in params_text or "get_current_user" in params_text
            return {"method": method.upper(), "path": path, "handler": handler, "requires_auth": requires_auth}
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
            "params": _node_text(node.child_by_field_name("parameters")),
            "return_type": _node_text(node.child_by_field_name("return_type")),
        }

    # ── TypeScript / JavaScript 파싱 ──────────────────────────────────────────

    def _parse_typescript(
        self, file_path: Path, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """TypeScript/JavaScript 파일을 파싱한다.

        Args:
            file_path: 파싱할 파일 경로.
            tree: tree-sitter 파싱 결과.

        Returns:
            (endpoints, functions, dependencies) 튜플.
        """
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

        path_str = str(file_path).replace("\\", "/")
        if "pages/api/" in path_str or "app/api/" in path_str:
            endpoints.extend(self._extract_nextjs_endpoints(file_path))

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
        return {"method": method.upper(), "path": path, "handler": ""}

    @staticmethod
    def _extract_nextjs_endpoints(file_path: Path) -> list[dict]:
        """Next.js 파일 경로 기반으로 엔드포인트를 생성한다."""
        path_str = str(file_path).replace("\\", "/")
        for marker in ("pages/api/", "app/api/"):
            if marker in path_str:
                api_part = path_str.split(marker, 1)[1]
                route = "/" + api_part.rsplit(".", 1)[0]
                route = route.replace("/index", "").replace("[", ":").replace("]", "")
                return [{"method": "ANY", "path": route, "handler": file_path.stem}]
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
            "params": _node_text(node.child_by_field_name("parameters")),
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
            "params": _node_text(params_node),
            "return_type": "",
        }

    # ── Java 파싱 ─────────────────────────────────────────────────────────────

    def _parse_java(
        self, file_path: Path, tree: Any
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Java 파일을 파싱하여 엔드포인트, 함수, 의존성을 추출한다.

        Args:
            file_path: 파싱할 파일 경로.
            tree: tree-sitter 파싱 결과.

        Returns:
            (endpoints, functions, dependencies) 튜플.
        """
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
        for ann in _find_nodes(method_node, "marker_annotation"):
            ann_name = _node_text(ann.child_by_field_name("name"))
            if ann_name in _JAVA_MAPPINGS:
                return {"method": _JAVA_MAPPINGS[ann_name] or "ANY", "path": "", "handler": handler}
        for ann in _find_nodes(method_node, "annotation"):
            ann_name = _node_text(ann.child_by_field_name("name"))
            if ann_name in _JAVA_MAPPINGS:
                return {
                    "method": _JAVA_MAPPINGS[ann_name] or "ANY",
                    "path": self._extract_java_annotation_value(ann),
                    "handler": handler,
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
            "params": _node_text(node.child_by_field_name("formal_parameters")),
            "return_type": _node_text(node.child_by_field_name("type")),
        }

    # ── 함수 호출 추출 ────────────────────────────────────────────────────────

    @staticmethod
    def _extract_calls_from_tree(node: Node, language: str) -> list[str]:
        """함수 노드에서 호출되는 함수 이름 목록을 추출한다.

        Args:
            node: 함수 AST 노드.
            language: 파일 언어.

        Returns:
            중복 제거된 함수 호출 이름 목록.
        """
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

    def _dispatch_extract_models(
        self, file_path: Path, language: str, tree: Any
    ) -> list[dict]:
        """언어별 데이터 모델 추출기를 호출한다."""
        if language == "python":
            return self._extract_models_python(tree)
        if language in ("typescript", "javascript"):
            return self._extract_models_typescript(tree)
        if language == "java":
            return self._extract_models_java(tree)
        return []

    @staticmethod
    def _extract_models_python(tree: Any) -> list[dict]:
        """Python BaseModel / TypedDict / dataclass 클래스를 추출한다.

        Args:
            tree: tree-sitter 파싱 결과.

        Returns:
            {"name", "fields", "source"} dict 목록.
        """
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
        """TypeScript interface / type alias 선언에서 모델을 추출한다.

        Args:
            tree: tree-sitter 파싱 결과.

        Returns:
            {"name", "fields", "source"} dict 목록.
        """
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
        """Java @Entity / @Data / @Table 클래스에서 모델을 추출한다.

        Args:
            tree: tree-sitter 파싱 결과.

        Returns:
            {"name", "fields", "source"} dict 목록.
        """
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

    # ── 스킵 체크 ─────────────────────────────────────────────────────────────

    def _check_skip(self, trigger: str, repo_path: Path) -> tuple[dict | None, str]:
        """트리거 조건에 따라 스캔 스킵 여부를 결정한다.

        Args:
            trigger: 실행 트리거.
            repo_path: 저장소 루트 경로.

        Returns:
            스킵 시 (결과 dict, ""), 계속 시 (None, 유효 트리거).
        """
        if trigger == "doc_update":
            return (
                {"scan_result": None, "_metadata": {"skipped": True, "reason": "doc_update"}},
                "",
            )
        if trigger == "natural_lang":
            manifest = self._load_manifest(repo_path)
            try:
                head_hash = git.Repo(repo_path).head.commit.hexsha
            except Exception:
                head_hash = ""
            if manifest and head_hash and manifest.get("last_commit_hash") == head_hash:
                return (
                    {
                        "scan_result": None,
                        "_metadata": {"skipped": True, "reason": "no_code_change"},
                    },
                    "",
                )
            return None, "code_change"
        return None, trigger

    # ── 설정 파일 파싱 ────────────────────────────────────────────────────────

    def _parse_yaml_config(self, tree: Any) -> list[str]:
        """YAML 설정 파일을 파싱하여 마스킹된 key=value 목록을 반환한다.

        Args:
            tree: tree-sitter 파싱 결과.

        Returns:
            마스킹된 "key=value" 문자열 목록.
        """
        result: list[str] = []
        for pair in _find_nodes(tree.root_node, "block_mapping_pair"):
            key = _node_text(pair.child_by_field_name("key")).strip()
            val_node = pair.child_by_field_name("value")
            val = _node_text(val_node).strip() if val_node else ""
            if key:
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    def _parse_env_file(self, file_path: Path) -> list[str]:
        """`.env` 파일을 정규식으로 파싱하여 마스킹된 key=value 목록을 반환한다.

        Args:
            file_path: .env 파일 경로.

        Returns:
            마스킹된 "key=value" 문자열 목록.
        """
        result: list[str] = []
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ENV_RE.match(line.strip())
            if m:
                key, val = m.group(1), m.group(2)
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    def _parse_properties_file(self, file_path: Path) -> list[str]:
        """`.properties` 파일을 파싱하여 마스킹된 key=value 목록을 반환한다.

        Args:
            file_path: .properties 파일 경로.

        Returns:
            마스킹된 "key=value" 문자열 목록.
        """
        result: list[str] = []
        for line in file_path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _PROP_RE.match(line)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
                result.append(f"{key}={self._mask_sensitive(key, val)}")
        return result

    @staticmethod
    def _mask_sensitive(key: str, value: str) -> str:
        """민감 키의 값을 마스킹한다.

        Args:
            key: 설정 키 이름.
            value: 설정 값.

        Returns:
            민감 키면 "***", 아니면 원본 value.
        """
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            return "***"
        return value

    # ── Git 이력 ──────────────────────────────────────────────────────────────

    def _extract_git_diff(self, repo_path: Path) -> GitDiff | None:
        """Git 이력을 추출하여 GitDiff를 반환한다.

        Args:
            repo_path: 저장소 루트 경로.

        Returns:
            GitDiff 또는 None (Git 미사용/오류 시).
        """
        try:
            repo = git.Repo(repo_path)
            # Ensure git paths are unquoted for Python to read them correctly, bypassing core.quotePath
            repo.git.config("core.quotePath", "false", local=True)
            
            commit = repo.head.commit
            current = commit.hexsha
            parents = commit.parents
            prev = parents[0].hexsha if parents else ""

            author = commit.author.name or ""
            author_email = commit.author.email or ""
            commit_timestamp = datetime.fromtimestamp(
                commit.authored_date, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            if prev:
                changed = repo.git.diff(f"{prev}..{current}", "--name-only")
                changed_files = [
                    f.strip('"') for f in changed.splitlines() if f.strip()
                ]
                stat = repo.git.diff(f"{prev}..{current}", "--stat")
                added, deleted = self._parse_stat(stat)
                diff_detail = self._parse_diff_detail(repo, prev, current)
            else:
                changed_files, added, deleted, diff_detail = [], 0, 0, []

            blame = self._extract_blame(repo, changed_files)

            return GitDiff(
                commit_hash=current,
                prev_hash=prev,
                changed_files=changed_files,
                added_lines=added,
                deleted_lines=deleted,
                diff_detail=diff_detail,
                author=author,
                author_email=author_email,
                commit_timestamp=commit_timestamp,
                blame=blame,
            )
        except Exception as e:
            self.logger.warning("git_unavailable", error=str(e))
            return None

    @staticmethod
    def _parse_stat(stat_output: str) -> tuple[int, int]:
        """git diff --stat 출력에서 추가/삭제 라인 수를 추출한다.

        Args:
            stat_output: git diff --stat 명령 출력.

        Returns:
            (added_lines, deleted_lines) 튜플.
        """
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

    def _parse_diff_detail(self, repo: Any, prev: str, current: str) -> list[dict]:
        """커밋 간 파일별 변경 라인 수를 추출한다.

        Args:
            repo: GitPython Repo 객체.
            prev: 이전 commit hash.
            current: 현재 commit hash.

        Returns:
            파일별 {"file", "added", "deleted"} dict 목록.
        """
        details: list[dict] = []
        try:
            raw = repo.git.diff(f"{prev}..{current}", "--numstat")
            for line in raw.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                added_s, deleted_s, path = parts[0], parts[1], parts[2]
                details.append({
                    "file": path,
                    "added": int(added_s) if added_s.isdigit() else 0,
                    "deleted": int(deleted_s) if deleted_s.isdigit() else 0,
                })
        except Exception as e:
            self.logger.warning("diff_detail_failed", error=str(e))
        return details

    def _extract_blame(self, repo: Any, changed_files: list[str]) -> list[dict]:
        """변경된 파일별 blame 요약을 추출한다.

        Args:
            repo: GitPython Repo 객체.
            changed_files: 변경된 파일 경로 목록.

        Returns:
            파일별 {"file", "author", "author_email", "timestamp"} dict 목록.
        """
        blame_summary: list[dict] = []
        for file_path in changed_files:
            try:
                blame_entries = repo.blame("HEAD", file_path)
                if blame_entries:
                    latest_commit = blame_entries[-1][0]
                    blame_summary.append({
                        "file": file_path,
                        "author": latest_commit.author.name or "",
                        "author_email": latest_commit.author.email or "",
                        "timestamp": datetime.fromtimestamp(
                            latest_commit.authored_date, tz=timezone.utc
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    })
            except Exception as e:
                self.logger.warning("blame_failed", file=file_path, error=str(e))
        return blame_summary

    # ── manifest ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_manifest(repo_path: Path) -> dict | None:
        """manifest.json을 읽어 반환한다.

        Args:
            repo_path: 저장소 루트 경로.

        Returns:
            manifest dict 또는 None (없거나 파싱 실패 시).
        """
        manifest_path = repo_path / ".qapilot" / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_manifest(
        self, repo_path: Path, commit_hash: str, scanned_files: list[str]
    ) -> None:
        """manifest.json을 갱신한다.

        Args:
            repo_path: 저장소 루트 경로.
            commit_hash: 현재 commit hash.
            scanned_files: 스캔된 파일 경로 목록.
        """
        qapilot_dir = repo_path / ".qapilot"
        qapilot_dir.mkdir(exist_ok=True)
        manifest = {
            "last_commit_hash": commit_hash,
            "scan_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "scanned_files": scanned_files,
        }
        (qapilot_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.logger.info("manifest_saved", files=len(scanned_files), commit=commit_hash)
