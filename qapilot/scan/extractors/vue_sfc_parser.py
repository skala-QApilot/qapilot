"""Vue SFC parser — frontend.selectors 추출 (PoC 2, 데이터 layer).

Node.js subprocess (qapilot/node-bridge/parse_vue_sfc.js) 으로 @vue/compiler-sfc
AST 를 받아, data-testid 가 있는 element 를 Pydantic model 로 변환.

분류 정책 (PoC 단계):
- input/textarea/select         → InputElement
- button                        → ButtonElement
- 그 외 (div/span/p/...)         → OutputElement
- 단 parent_v_if 가 존재하면 → DynamicElement 로 재분류
  (예: `<div v-if="isMinor"><input data-testid="guardian-consent">` 의 input)

본 모듈은 단일 .vue 파일 단위. service 전체 walk + FrontendSelectorsIndex 생성은
PoC 5+ caller 가 담당.

Vue AST NodeTypes (@vue/compiler-core):
  0=ROOT, 1=ELEMENT, 2=TEXT, 3=COMMENT, 4=SIMPLE_EXPRESSION,
  5=INTERPOLATION, 6=ATTRIBUTE, 7=DIRECTIVE, 8=COMPOUND_EXPRESSION,
  9=IF, 10=IF_BRANCH, 11=FOR, 12=TEXT_CALL.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from qapilot.shared.metadata_schemas import (
    ButtonElement,
    DisabledWhen,
    DynamicElement,
    ExtractedFrom,
    InputElement,
    OutputElement,
)

logger = logging.getLogger(__name__)

_BRIDGE_DIR = Path(__file__).resolve().parents[2] / "node-bridge"
_BRIDGE_SCRIPT = _BRIDGE_DIR / "parse_vue_sfc.js"

# AST NodeTypes
_NODE_ELEMENT = 1
_PROP_ATTRIBUTE = 6
_PROP_DIRECTIVE = 7

# 입력 element tag
_INPUT_TAGS = {"input", "textarea", "select"}
_BUTTON_TAGS = {"button"}

ExtractedElement = InputElement | ButtonElement | OutputElement | DynamicElement


class VueSfcParseError(Exception):
    """node-bridge subprocess 실패 (graceful caller — 빈 list fallback 가능)."""


def parse_vue_file(
    file_path: Path,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """node-bridge 호출 — raw parse result 반환.

    Returns:
        parse_vue_sfc.js 의 stdout JSON ({ok, template, script?, errors}).

    Raises:
        VueSfcParseError: subprocess crash / timeout / ok=false.
    """
    if not _BRIDGE_SCRIPT.exists():
        raise VueSfcParseError(
            f"node-bridge script missing: {_BRIDGE_SCRIPT}. "
            "Run `npm install` in qapilot/node-bridge/."
        )

    resolved = file_path.resolve()
    if not resolved.exists():
        raise VueSfcParseError(f"File not found: {resolved}")

    try:
        completed = subprocess.run(
            ["node", str(_BRIDGE_SCRIPT), str(resolved)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise VueSfcParseError(f"Timed out after {timeout}s parsing {resolved}") from e
    except FileNotFoundError as e:
        raise VueSfcParseError(
            "`node` binary not found on PATH — install Node.js (>=18) on this host."
        ) from e

    if completed.returncode != 0 and not completed.stdout:
        raise VueSfcParseError(
            f"node-bridge exited rc={completed.returncode}, stderr={completed.stderr[:500]}"
        )

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise VueSfcParseError(
            f"node-bridge returned invalid JSON: {e}. stdout head={completed.stdout[:200]}"
        ) from e

    if not result.get("ok"):
        raise VueSfcParseError(
            f"@vue/compiler-sfc parse failed: {result.get('error', '?')}"
        )

    return result


# ────────────────────────────────────────────────────────────────────────
# AST helpers
# ────────────────────────────────────────────────────────────────────────

def _get_attr(props: list[dict], name: str) -> str | None:
    """data-testid / type / placeholder 등 정적 ATTRIBUTE 값 추출."""
    for p in props or []:
        if p.get("type") == _PROP_ATTRIBUTE and p.get("name") == name:
            val = p.get("value")
            if isinstance(val, dict):
                return val.get("content")
            return val
    return None


def _has_attr(props: list[dict], name: str) -> bool:
    for p in props or []:
        if p.get("type") == _PROP_ATTRIBUTE and p.get("name") == name:
            return True
    return False


def _get_directive_exp(props: list[dict], dir_name: str) -> str | None:
    """v-if / v-show / v-model 등 DIRECTIVE 의 expression 추출."""
    for p in props or []:
        if p.get("type") == _PROP_DIRECTIVE and p.get("name") == dir_name:
            exp = p.get("exp")
            if isinstance(exp, dict):
                return exp.get("content")
    return None


def _get_bind_arg(props: list[dict], arg_name: str) -> str | None:
    """v-bind:<arg> 또는 :<arg> 의 expression 추출 (예: :disabled='...')."""
    for p in props or []:
        if p.get("type") == _PROP_DIRECTIVE and p.get("name") == "bind":
            arg = p.get("arg")
            if isinstance(arg, dict) and arg.get("content") == arg_name:
                exp = p.get("exp")
                if isinstance(exp, dict):
                    return exp.get("content")
    return None


def _loc_lines(node: dict) -> tuple[int, int]:
    loc = node.get("loc") or {}
    start = loc.get("start") or {}
    end = loc.get("end") or {}
    return int(start.get("line", 1)), int(end.get("line", 1))


# ────────────────────────────────────────────────────────────────────────
# AST walker
# ────────────────────────────────────────────────────────────────────────

def _walk(
    node: dict,
    *,
    parent_v_if: str | None,
    relative_file: str,
    commit_sha: str,
    out: list[ExtractedElement],
) -> None:
    """재귀 walk — data-testid 가 있는 ELEMENT 를 out 에 누적.

    parent_v_if 는 부모 트리에서 가장 가까운 v-if/v-else-if 표현식.
    """
    if not isinstance(node, dict):
        return

    node_type = node.get("type")

    # ELEMENT 만 처리. ROOT (type=0) 은 children 만 내려간다.
    if node_type == _NODE_ELEMENT:
        tag = (node.get("tag") or "").lower()
        props = node.get("props") or []
        testid = _get_attr(props, "data-testid")

        # 자체 v-if (DIRECTIVE name='if')
        self_v_if = _get_directive_exp(props, "if")
        if self_v_if is None:
            self_v_if = _get_directive_exp(props, "else-if")

        # 자식으로 내려갈 때 사용할 effective v-if
        effective_v_if_for_children = self_v_if or parent_v_if

        if testid:
            line_start, line_end = _loc_lines(node)
            extracted_from = ExtractedFrom(
                file=relative_file,
                line_start=line_start,
                line_end=line_end,
                commit_sha=commit_sha,
            )
            # parent_v_if 가 존재하면 dynamic. self_v_if 만 있는 경우는
            # element 자체 v-if 로 충분 (Output/Input 에 v_if 채움).
            if parent_v_if is not None:
                out.append(DynamicElement(
                    extracted_from=extracted_from,
                    confidence=1.0,
                    extraction_method="ast",
                    testid=testid,
                    html_tag=tag,
                    html_type=_get_attr(props, "type"),
                    v_if=parent_v_if,
                    semantic_purpose=None,
                ))
            elif tag in _INPUT_TAGS:
                validators: list[str] = []
                if _has_attr(props, "required"):
                    validators.append("required")
                html_type = _get_attr(props, "type")
                if html_type == "email":
                    validators.append("email")
                out.append(InputElement(
                    extracted_from=extracted_from,
                    confidence=1.0,
                    extraction_method="ast",
                    testid=testid,
                    html_type=html_type,
                    required=_has_attr(props, "required"),
                    v_model=_get_directive_exp(props, "model"),
                    label=None,
                    placeholder=_get_attr(props, "placeholder"),
                    validators=validators,
                ))
            elif tag in _BUTTON_TAGS:
                disabled_expr = _get_bind_arg(props, "disabled")
                disabled_when = None
                if disabled_expr:
                    disabled_when = DisabledWhen(
                        expr=disabled_expr,
                        semantic=None,
                        confidence_for_semantic=None,
                        extraction_method_for_semantic=None,
                    )
                out.append(ButtonElement(
                    extracted_from=extracted_from,
                    confidence=1.0,
                    extraction_method="ast",
                    testid=testid,
                    html_type=_get_attr(props, "type"),
                    form_role=_get_attr(props, "type"),
                    disabled_when=disabled_when,
                    label=None,
                ))
            else:
                out.append(OutputElement(
                    extracted_from=extracted_from,
                    confidence=1.0,
                    extraction_method="ast",
                    testid=testid,
                    html_tag=tag,
                    v_if=self_v_if,
                    semantic_kind=None,
                    semantic_purpose=None,
                ))

        # 자식 재귀
        for child in node.get("children") or []:
            _walk(child, parent_v_if=effective_v_if_for_children,
                  relative_file=relative_file, commit_sha=commit_sha, out=out)
        return

    # ROOT (type=0) 또는 그 외 — children 만 내려가서 ELEMENT 찾는다.
    for child in node.get("children") or []:
        _walk(child, parent_v_if=parent_v_if,
              relative_file=relative_file, commit_sha=commit_sha, out=out)


# ────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────

def extract_selectors_from_vue(
    file_path: Path,
    *,
    repo_root: Path,
    commit_sha: str,
    timeout: float = 10.0,
) -> list[ExtractedElement]:
    """단일 .vue 파일에서 data-testid element 추출.

    Args:
        file_path: 절대 경로.
        repo_root: service repo root — extracted_from.file 의 상대 경로 계산.
        commit_sha: 풀 git SHA (40자).
        timeout: subprocess timeout (초).

    Returns:
        InputElement / ButtonElement / OutputElement / DynamicElement 의 리스트.
        data-testid 없는 element 는 포함하지 않는다.

    Raises:
        VueSfcParseError: parse 실패 (caller 가 graceful fallback 결정).
    """
    parsed = parse_vue_file(file_path, timeout=timeout)
    template = parsed.get("template") or {}
    ast = template.get("ast")
    if ast is None:
        logger.debug("vue_sfc_no_template", extra={"file": str(file_path)})
        return []

    resolved = file_path.resolve()
    try:
        relative_file = str(resolved.relative_to(repo_root.resolve()))
    except ValueError:
        # repo_root 외부 파일 — 절대 경로 fallback (caller 의 잘못된 입력)
        relative_file = str(resolved)
        logger.warning(
            "vue_sfc_file_outside_repo_root",
            extra={"file": str(resolved), "repo_root": str(repo_root)},
        )

    out: list[ExtractedElement] = []
    _walk(ast, parent_v_if=None, relative_file=relative_file,
          commit_sha=commit_sha, out=out)
    return out
