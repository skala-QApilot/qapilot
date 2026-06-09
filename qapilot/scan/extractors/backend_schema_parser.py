"""backend.schemas 추출기 — Pydantic + SQLAlchemy (PoC 6.A, 데이터 layer).

추출 대상 (framework 일반, 도메인 무관):
- Pydantic v1/v2 `class XxxRequest(BaseModel):` / `class XxxResponse(BaseModel):`
- SQLAlchemy 2.x `class Xxx(Base):` (Mapped[type] = mapped_column(...))
- SQLAlchemy legacy `class Xxx(Base):` (= Column(...))

framework 분기:
- BaseModel 상속 → RequestSchema / ResponseSchema
   - 클래스명에 'Response/Out' 포함 → response, 아니면 request (단순 휴리스틱)
- Base / DeclarativeBase 상속 → DbModel

본 추출기는 단일 .py 파일 단위. service 전체 walk + BackendSchemasIndex 생성은
PoC 6+ caller 가 담당.

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from qapilot.shared.metadata_schemas import (
    DbModel,
    DbModelColumn,
    ExtractedFrom,
    RequestSchema,
    ResponseSchema,
    SchemaField,
    SchemaValidator,
    StatusCode,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Parser singleton
# ────────────────────────────────────────────────────────────────────────

_parser_lock = threading.Lock()
_parser: Parser | None = None


def _get_parser() -> Parser:
    global _parser
    if _parser is not None:
        return _parser
    with _parser_lock:
        if _parser is None:
            _parser = Parser(Language(tspython.language()))
    return _parser


def _node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


# ────────────────────────────────────────────────────────────────────────
# 상속 검사
# ────────────────────────────────────────────────────────────────────────

_PYDANTIC_BASES = {"BaseModel"}
_SQLA_BASES = {"Base", "DeclarativeBase"}


def _superclass_names(class_node: Node, source: bytes) -> list[str]:
    """class_definition 의 superclasses 이름 list."""
    args = class_node.child_by_field_name("superclasses")
    if args is None:
        return []
    names: list[str] = []
    for child in args.children:
        if child.type == "identifier":
            names.append(_node_text(child, source))
        elif child.type == "attribute":
            # pkg.Base 같은 경우 — 마지막 부분만
            text = _node_text(child, source)
            names.append(text.rsplit(".", 1)[-1])
    return names


# ────────────────────────────────────────────────────────────────────────
# Pydantic field 추출
# ────────────────────────────────────────────────────────────────────────

# Pydantic Field(...) 인자 정규식 — keyword 만 추출 (PoC, AST 깊이 파기보다 효율적)
# raw string (r"...") + double/single quoted + bare word 지원
_FIELD_KW_RE = re.compile(
    r'(\w+)\s*=\s*('
    r'[rR]?"(?:[^"\\]|\\.)*"'      # optional r/R prefix + double-quoted string
    r"|[rR]?'(?:[^'\\]|\\.)*'"     # optional r/R prefix + single-quoted string
    r'|[\w.\-]+'                    # bare word (123, True, Var.attr 등)
    r')'
)


def _strip_string_literal(v: str) -> str:
    """raw string prefix + quote 제거 — `r"..."`, `R'...'`, `"..."`, `'...'` 모두."""
    if not v:
        return v
    # r/R prefix
    if v[0] in ("r", "R") and len(v) > 1 and v[1] in ('"', "'"):
        v = v[1:]
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    return v


def _parse_pydantic_field_args(field_call_text: str) -> dict[str, Any]:
    """Field(...) 호출의 keyword args 추출 — text 기반 단순화."""
    # Field(...) 안의 내용 추출
    m = re.search(r"Field\s*\((.*)\)\s*$", field_call_text, re.DOTALL)
    if not m:
        return {}
    inner = m.group(1)
    out: dict[str, Any] = {}
    for kw_m in _FIELD_KW_RE.finditer(inner):
        k = kw_m.group(1)
        v = kw_m.group(2)
        out[k] = _strip_string_literal(v)
    return out


def _pydantic_field_to_validators(field_args: dict[str, Any]) -> list[SchemaValidator]:
    """Field(...) 의 keyword 인자 → SchemaValidator list."""
    out: list[SchemaValidator] = []
    for k in ("min_length", "max_length", "ge", "gt", "le", "lt"):
        if k in field_args:
            try:
                out.append(SchemaValidator(kind=k, value=int(field_args[k])))
            except ValueError:
                pass
    if "pattern" in field_args:
        out.append(SchemaValidator(kind="regex", value=field_args["pattern"]))
    return out


def _is_required_pydantic(field_args: dict[str, Any], has_default: bool) -> bool:
    """Pydantic field 의 required 결정.

    - `Field(...)` 또는 `Field(..., ...)` 의 `...` (default 가 Ellipsis) = required
    - `Field(default=X)` 또는 `Field(default_factory=X)` = optional
    - default 자체가 없으면 (annotation 만) = required
    - `Field` 없이 default value 있으면 optional
    """
    if "default" in field_args or "default_factory" in field_args:
        return False
    return not has_default


def _extract_pydantic_fields(
    class_node: Node, source: bytes,
) -> list[SchemaField]:
    """Pydantic class body 에서 typed annotations 추출 → SchemaField list."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    out: list[SchemaField] = []
    for stmt in body.children:
        # `name: type = value` 또는 `name: type` 패턴
        if stmt.type != "expression_statement":
            continue
        target_node = stmt.named_child(0) if stmt.named_child_count else None
        if target_node is None or target_node.type != "assignment":
            # annotation only (no default) — `name: type`
            if target_node is not None and target_node.type == "type":
                # tree-sitter 가 type 으로만 표기하는 경우 (rare)
                continue
            # `name: type` 인 경우 — expression_statement -> assignment 가 아닌 형태
            # tree-sitter-python 에서는 sub-pattern 으로 표현됨, 우선 assignment 만 처리
            continue

        name_node = target_node.child_by_field_name("left")
        type_node = target_node.child_by_field_name("type")
        value_node = target_node.child_by_field_name("right")

        if name_node is None or name_node.type != "identifier":
            continue
        if type_node is None:
            # annotation 없는 일반 assignment 는 schema field 아님 — skip
            continue

        name = _node_text(name_node, source)
        if name.startswith("_") or name == "model_config":
            continue

        type_text = _node_text(type_node, source).lstrip(":").strip()
        # `type: int | None` 의 ` | None` 제거 (nullable 만 따로 표기)
        nullable = "None" in type_text and "|" in type_text
        clean_type = type_text.replace(" | None", "").replace(" | none", "").strip()

        field_args: dict[str, Any] = {}
        has_default = value_node is not None
        if value_node is not None:
            value_text = _node_text(value_node, source)
            if "Field(" in value_text:
                field_args = _parse_pydantic_field_args(value_text)
                # Field(...) — 첫 인자 ellipsis = required
                if re.match(r"Field\s*\(\s*\.\.\.\s*[,)]", value_text):
                    has_default = False
                # Field(min_length=3) — default/default_factory keyword 없으면 required
                elif "default" not in field_args and "default_factory" not in field_args:
                    has_default = False
            # default = None 인 경우 nullable
            if value_text.strip() == "None":
                nullable = True

        validators = _pydantic_field_to_validators(field_args)

        # extracted_from 은 caller 가 set (file/line)
        out.append(SchemaField(
            extracted_from=ExtractedFrom(
                file="(set by caller)", line_start=name_node.start_point[0] + 1,
                line_end=name_node.end_point[0] + 1, commit_sha="(set by caller)",
            ),
            confidence=1.0, extraction_method="ast",
            name=name, type=clean_type,
            required=_is_required_pydantic(field_args, has_default),
            validators=validators,
            nullable=nullable,
            sensitive=("password" in name.lower()),  # 휴리스틱 (도메인 무관 sensitive 표기)
        ))
    return out


# ────────────────────────────────────────────────────────────────────────
# SQLAlchemy column 추출 (Mapped + legacy 둘 다)
# ────────────────────────────────────────────────────────────────────────

_SQLA_TYPE_MAP = {
    "Integer": "int", "BigInteger": "int", "SmallInteger": "int",
    "String": "str", "Text": "str", "VARCHAR": "str",
    "Boolean": "bool",
    "Date": "date", "DateTime": "datetime", "Time": "time",
    "Numeric": "decimal", "Float": "float",
    "JSON": "dict", "JSONB": "dict",
    "UUID": "uuid",
}


def _sqla_type_from_call(call_text: str) -> str:
    """`mapped_column(Integer, ...)` 또는 `Column(String(255), ...)` 에서 type 추출."""
    # 첫 인자 추출
    m = re.search(r"(?:mapped_column|Column)\s*\(\s*(\w+)", call_text)
    if not m:
        return "any"
    sqla_type = m.group(1)
    return _SQLA_TYPE_MAP.get(sqla_type, sqla_type.lower())


def _sqla_kw_args(call_text: str) -> dict[str, Any]:
    """mapped_column / Column 의 keyword 인자 추출."""
    out: dict[str, Any] = {}
    inner_match = re.search(r"(?:mapped_column|Column)\s*\((.*)\)", call_text, re.DOTALL)
    if not inner_match:
        return out
    inner = inner_match.group(1)
    for kw_m in _FIELD_KW_RE.finditer(inner):
        out[kw_m.group(1)] = _strip_string_literal(kw_m.group(2))
    return out


def _extract_table_name(class_node: Node, source: bytes) -> str | None:
    """class body 에서 __tablename__ = "..." 추출."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return None
    for stmt in body.children:
        if stmt.type != "expression_statement":
            continue
        target = stmt.named_child(0)
        if target is None or target.type != "assignment":
            continue
        left = target.child_by_field_name("left")
        right = target.child_by_field_name("right")
        if left is not None and right is not None and _node_text(left, source) == "__tablename__":
            text = _node_text(right, source).strip()
            if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
                return text[1:-1]
    return None


def _extract_sqla_columns(class_node: Node, source: bytes) -> list[DbModelColumn]:
    """SQLAlchemy class 의 column 들 추출 (Mapped + legacy)."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    out: list[DbModelColumn] = []
    for stmt in body.children:
        if stmt.type != "expression_statement":
            continue
        target = stmt.named_child(0)
        if target is None or target.type != "assignment":
            continue

        left = target.child_by_field_name("left")
        type_node = target.child_by_field_name("type")
        right = target.child_by_field_name("right")
        if left is None or right is None:
            continue
        name = _node_text(left, source)
        if name.startswith("_") or name in ("__tablename__",):
            continue

        right_text = _node_text(right, source)
        # SQLAlchemy column 만 — mapped_column 또는 Column 호출
        if "mapped_column(" not in right_text and "Column(" not in right_text:
            continue

        # type 추출 (Mapped[int] 우선, 없으면 mapped_column/Column 첫 인자)
        sqla_type = _sqla_type_from_call(right_text)
        if type_node is not None:
            type_text = _node_text(type_node, source).lstrip(":").strip()
            m = re.match(r"Mapped\[(.+)\]", type_text)
            if m:
                inner = m.group(1)
                nullable = "None" in inner and "|" in inner
                clean = inner.replace(" | None", "").replace(" | none", "").strip()
                sqla_type = clean  # 우선
            else:
                nullable = False
        else:
            nullable = False

        kw = _sqla_kw_args(right_text)
        primary_key = str(kw.get("primary_key", "")).lower() == "true"
        unique = str(kw.get("unique", "")).lower() == "true"
        nullable_kw = str(kw.get("nullable", "")).lower()
        if nullable_kw in ("true", "false"):
            nullable = nullable_kw == "true"
        default_val = kw.get("default") or kw.get("server_default")

        out.append(DbModelColumn(
            extracted_from=ExtractedFrom(
                file="(set by caller)", line_start=left.start_point[0] + 1,
                line_end=left.end_point[0] + 1, commit_sha="(set by caller)",
            ),
            confidence=1.0, extraction_method="ast",
            name=name, type=sqla_type,
            required=not nullable and not primary_key,  # auto-PK 는 required X
            nullable=nullable,
            primary_key=primary_key,
            unique=unique,
            default=default_val,
            sensitive=("password" in name.lower()),
        ))
    return out


# ────────────────────────────────────────────────────────────────────────
# Class 분류
# ────────────────────────────────────────────────────────────────────────

def _is_response_class(name: str) -> bool:
    """클래스명 휴리스틱 — Response/Out/Reply/Result 면 response."""
    n = name.lower()
    return n.endswith("response") or n.endswith("out") or n.endswith("reply") or n.endswith("result")


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

def extract_backend_schemas_from_file(
    file_path: Path,
    *,
    repo_root: Path,
    commit_sha: str,
) -> tuple[dict[str, RequestSchema], dict[str, ResponseSchema], dict[str, DbModel]]:
    """단일 .py 파일에서 Pydantic + SQLAlchemy class 추출.

    Returns:
        (request_schemas, response_schemas, db_models) 튜플.
        각 dict 는 {class_name: schema/model}.

    graceful:
        파일 read/parse 실패 = 빈 dict 3개.
    """
    resolved = file_path.resolve()
    try:
        source = resolved.read_bytes()
    except OSError as e:
        logger.warning("backend_schema_read_failed",
                       extra={"file": str(resolved), "error": str(e)})
        return {}, {}, {}

    try:
        tree = _get_parser().parse(source)
    except Exception as e:
        logger.warning("backend_schema_parse_failed",
                       extra={"file": str(resolved), "error": str(e)})
        return {}, {}, {}

    try:
        relative_file = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        relative_file = str(resolved)

    req_schemas: dict[str, RequestSchema] = {}
    resp_schemas: dict[str, ResponseSchema] = {}
    db_models: dict[str, DbModel] = {}

    def walk(node: Node):
        if node.type == "class_definition":
            _process_class(node, source, relative_file, commit_sha,
                           req_schemas, resp_schemas, db_models)
            # 중첩 class 도 처리 (drop down)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return req_schemas, resp_schemas, db_models


def _process_class(
    class_node: Node, source: bytes, relative_file: str, commit_sha: str,
    req_schemas: dict, resp_schemas: dict, db_models: dict,
) -> None:
    name_node = class_node.child_by_field_name("name")
    if name_node is None:
        return
    class_name = _node_text(name_node, source)
    superclasses = _superclass_names(class_node, source)

    is_pydantic = any(s in _PYDANTIC_BASES for s in superclasses)
    is_sqla = any(s in _SQLA_BASES for s in superclasses)

    if is_pydantic:
        fields = _extract_pydantic_fields(class_node, source)
        # extracted_from 의 file/commit 채움
        for f in fields:
            f.extracted_from = ExtractedFrom(
                file=relative_file, line_start=f.extracted_from.line_start,
                line_end=f.extracted_from.line_end, commit_sha=commit_sha,
            )
        if _is_response_class(class_name):
            # 단일 status 200 default — body 미상은 fields 만 표기
            resp_schemas[class_name] = ResponseSchema(status_codes=[
                StatusCode(code=200, description="OK", body={"fields": [f.model_dump() for f in fields]}),
            ])
        else:
            req_schemas[class_name] = RequestSchema(fields=fields)
        return

    if is_sqla:
        cols = _extract_sqla_columns(class_node, source)
        for c in cols:
            c.extracted_from = ExtractedFrom(
                file=relative_file, line_start=c.extracted_from.line_start,
                line_end=c.extracted_from.line_end, commit_sha=commit_sha,
            )
        table_name = _extract_table_name(class_node, source) or class_name.lower()
        db_models[class_name] = DbModel(
            extracted_from=ExtractedFrom(
                file=relative_file, line_start=class_node.start_point[0] + 1,
                line_end=class_node.end_point[0] + 1, commit_sha=commit_sha,
            ),
            confidence=1.0, extraction_method="ast",
            table_name=table_name, columns=cols,
        )
