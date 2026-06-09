"""Vue Router 추출기 — frontend.routes (PoC 6.B, 데이터 layer).

추출 대상: Vue Router 의 routes 배열.
지원 패턴 (Vue Router 3/4 공통):

    const routes = [
      { path: '/dashboard', component: Dashboard, meta: { requiresAuth: true } },
      { path: '/plans/:id', component: PlanDetail },
      ...
    ]
    createRouter({ history: ..., routes })

다음 정보 추출:
- path (string)
- component_name (string, import 추적)
- params (path 의 `:name` 패턴)
- guards (meta.requiresAuth → "requireAuth" guard 명)
- redirect (string redirect 만, 함수 redirect 는 source 그대로)
- meta (dict, raw)

본 추출기는 단일 .js/.ts 파일 단위. caller 는 FrontendRoutesIndex 구성.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from qapilot.shared.metadata_schemas import (
    ExtractedFrom,
    RouteGuard,
    RouteParam,
    RouteRecord,
)

logger = logging.getLogger(__name__)

_PATH_PARAM_RE = re.compile(r":(\w+)(\?)?")


# ────────────────────────────────────────────────────────────────────────
# Parser singletons (JS + TS 별도)
# ────────────────────────────────────────────────────────────────────────

_parser_lock = threading.Lock()
_js_parser: Parser | None = None
_ts_parser: Parser | None = None


def _get_parser(lang: str) -> Parser:
    """lang: 'js' | 'ts'."""
    global _js_parser, _ts_parser
    with _parser_lock:
        if lang == "js":
            if _js_parser is None:
                _js_parser = Parser(Language(tsjs.language()))
            return _js_parser
        if lang == "ts":
            if _ts_parser is None:
                _ts_parser = Parser(Language(tsts.language_typescript()))
            return _ts_parser
        raise ValueError(f"unsupported lang: {lang}")


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'") or (s[0] == "`" and s[-1] == "`")):
        return s[1:-1]
    return s


# ────────────────────────────────────────────────────────────────────────
# routes 배열 찾기
# ────────────────────────────────────────────────────────────────────────

def _find_routes_array(root: Node, source: bytes) -> Node | None:
    """`const routes = [...]` 또는 `createRouter({routes: [...]})` 에서 array 노드 반환.

    우선순위:
    1. `routes` 로 명명된 const/let/var 의 array literal
    2. createRouter call argument 안의 `routes:` property 의 array
    """
    # 1. const/let/var routes = [...]
    arr = _find_named_array_declaration(root, source, "routes")
    if arr is not None:
        return arr

    # 2. createRouter({ routes: [...] })
    return _find_routes_in_create_router(root, source)


def _find_named_array_declaration(root: Node, source: bytes, name: str) -> Node | None:
    """`const <name> = [...]` 의 array 노드 반환."""
    found: list[Node] = []

    def walk(node: Node):
        if node.type == "variable_declarator":
            n = node.child_by_field_name("name")
            v = node.child_by_field_name("value")
            if (n is not None and v is not None
                    and _node_text(n, source) == name and v.type == "array"):
                found.append(v)
                return
        for child in node.children:
            walk(child)

    walk(root)
    return found[0] if found else None


def _find_routes_in_create_router(root: Node, source: bytes) -> Node | None:
    """`createRouter({routes: [...]})` 안의 array 노드."""
    found: list[Node] = []

    def walk(node: Node):
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and _node_text(callee, source) == "createRouter":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    # arguments 안의 첫 object literal 의 routes property
                    for arg in args.children:
                        if arg.type == "object":
                            for prop in arg.children:
                                if prop.type != "pair":
                                    continue
                                key = prop.child_by_field_name("key")
                                value = prop.child_by_field_name("value")
                                if (key is not None and value is not None
                                        and _node_text(key, source).strip("\"'`") == "routes"
                                        and value.type == "array"):
                                    found.append(value)
        for child in node.children:
            walk(child)

    walk(root)
    return found[0] if found else None


# ────────────────────────────────────────────────────────────────────────
# Object literal → RouteRecord
# ────────────────────────────────────────────────────────────────────────

def _object_properties(obj_node: Node, source: bytes) -> dict[str, Node]:
    """object literal 의 key→value node 매핑."""
    out: dict[str, Node] = {}
    for child in obj_node.children:
        if child.type != "pair":
            continue
        key = child.child_by_field_name("key")
        value = child.child_by_field_name("value")
        if key is None or value is None:
            continue
        key_text = _node_text(key, source).strip("\"'`")
        out[key_text] = value
    return out


def _extract_params(path: str) -> list[RouteParam]:
    """path 의 `:name` 또는 `:name?` 추출."""
    out: list[RouteParam] = []
    for m in _PATH_PARAM_RE.finditer(path):
        name = m.group(1)
        optional = m.group(2) == "?"
        out.append(RouteParam(name=name, type="string", optional=optional))
    return out


def _parse_meta_to_guards(meta_node: Node, source: bytes) -> list[RouteGuard]:
    """meta object 의 requiresAuth/requiresRole 등 → RouteGuard list."""
    out: list[RouteGuard] = []
    props = _object_properties(meta_node, source)
    if "requiresAuth" in props:
        val = _node_text(props["requiresAuth"], source).strip()
        if val == "true":
            out.append(RouteGuard(name="requireAuth", kind="meta"))
    if "requiresRole" in props:
        out.append(RouteGuard(
            name="requireRole",
            kind=f"meta:{_strip_quotes(_node_text(props['requiresRole'], source))}",
        ))
    if "requiresAdmin" in props:
        out.append(RouteGuard(name="requireAdmin", kind="meta"))
    return out


def _object_to_dict_raw(obj_node: Node, source: bytes) -> dict[str, Any]:
    """object literal → 단순 dict (key: string, value: raw text)."""
    out: dict[str, Any] = {}
    for k, v in _object_properties(obj_node, source).items():
        text = _node_text(v, source).strip()
        # 단순 boolean/number/string 처리
        if text == "true":
            out[k] = True
        elif text == "false":
            out[k] = False
        elif text.isdigit():
            out[k] = int(text)
        elif (text.startswith('"') or text.startswith("'") or text.startswith("`")):
            out[k] = _strip_quotes(text)
        else:
            out[k] = text  # 객체/함수 등 raw text
    return out


def _route_object_to_record(
    obj_node: Node, source: bytes, relative_file: str, commit_sha: str,
) -> RouteRecord | None:
    """object literal → RouteRecord."""
    props = _object_properties(obj_node, source)
    if "path" not in props:
        return None  # path 없는 object 는 route 아님

    path_text = _strip_quotes(_node_text(props["path"], source))

    component_name = None
    component_file = None
    if "component" in props:
        comp_text = _node_text(props["component"], source).strip()
        if comp_text and not comp_text.startswith("("):  # 함수 redirect 등 제외
            component_name = comp_text

    redirects_when_authed = None
    if "redirect" in props:
        red_text = _node_text(props["redirect"], source).strip()
        if red_text.startswith('"') or red_text.startswith("'"):
            redirects_when_authed = _strip_quotes(red_text)

    guards: list[RouteGuard] = []
    meta_dict: dict[str, Any] = {}
    if "meta" in props and props["meta"].type == "object":
        guards = _parse_meta_to_guards(props["meta"], source)
        meta_dict = _object_to_dict_raw(props["meta"], source)

    params = _extract_params(path_text)

    return RouteRecord(
        extracted_from=ExtractedFrom(
            file=relative_file,
            line_start=obj_node.start_point[0] + 1,
            line_end=obj_node.end_point[0] + 1,
            commit_sha=commit_sha,
        ),
        confidence=1.0, extraction_method="ast",
        path=path_text,
        component_file=component_file,
        component_name=component_name,
        guards=guards,
        redirects_when_authed=redirects_when_authed,
        meta=meta_dict,
        params=params,
        query_params=[],
    )


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

def extract_routes_from_router_file(
    file_path: Path,
    *,
    repo_root: Path,
    commit_sha: str,
) -> list[RouteRecord]:
    """Vue Router 정의 파일 (.js/.ts) 에서 routes 추출.

    Args:
        file_path: 절대 경로. `.js` 또는 `.ts` 만 처리.
        repo_root: relative path 계산.
        commit_sha: 풀 git SHA.

    Returns:
        RouteRecord 리스트. routes 배열 못 찾으면 빈 list.
        파일 read/parse 실패도 빈 list (graceful).
    """
    resolved = file_path.resolve()
    ext = resolved.suffix.lower()
    if ext not in (".js", ".ts"):
        return []

    try:
        source = resolved.read_bytes()
    except OSError as e:
        logger.warning("vue_router_read_failed",
                       extra={"file": str(resolved), "error": str(e)})
        return []

    lang = "ts" if ext == ".ts" else "js"
    try:
        tree = _get_parser(lang).parse(source)
    except Exception as e:
        logger.warning("vue_router_parse_failed",
                       extra={"file": str(resolved), "error": str(e)})
        return []

    routes_arr = _find_routes_array(tree.root_node, source)
    if routes_arr is None:
        return []

    try:
        relative_file = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        relative_file = str(resolved)

    out: list[RouteRecord] = []
    for child in routes_arr.children:
        if child.type != "object":
            continue
        rec = _route_object_to_record(child, source, relative_file, commit_sha)
        if rec is not None:
            out.append(rec)

    return out
