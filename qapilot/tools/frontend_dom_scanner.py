"""Frontend DOM 정적 인덱싱 helper (이슈 #127).

Vue SFC (`.vue`) / React TSX (`.tsx`) / React JSX (`.jsx`) 파일의 의미적 element
(input/button/textarea/select/label/a + `[role="button"]`) 의 속성을 정적 분석으로
추출해 `.qapilot/codebase-index/frontend.json` 으로 저장한다.

목적:
- ActionMapper LLM 호출 컨텍스트에 주입 → LLM 이 selector 추측 대신 실제 DOM 정보 참조
- e2e 의 UI 100% fail (trace `e1796b43` / `4e1d8d49`) 원인 분석 결과 (LLM 환각) 의
  근본 해결. 상세 배경: `docs/frontend-dom-scan-gap.md`, `docs/원인_분석.md`

접근: tree-sitter 대신 **regex 휴리스틱**.
- Vue SFC 의 `<template>` 부분은 HTML-like. tsx/jsx 는 JSX-like.
- 정확도 100% 아니나 의미적 element 80%+ 적중. 못 찾은 element 는 runtime DOM scan
  (UITestTool 옵션 B) 가 보완.
- tree-sitter-vue 같은 추가 dep 회피 (의존성 최소화).

담당: D (본인 영역 인접 — UITestTool 의 정적 분석 보완)
이슈 #127 (2026-05-19): 통합 e2e 의 UI 환각 근본 해결.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TypedDict

# Frontend 디렉토리 후보 — qapilot init `_infer_target_url` 과 동일 순서.
_FE_DIR_CANDIDATES: tuple[str, ...] = ("frontend", "web", "client", "ui", "app", "src")

# 스캔 대상 확장자.
_FE_EXTENSIONS: tuple[str, ...] = (".vue", ".tsx", ".jsx")

# 추출 대상 element — UI 검증·조작 가능한 의미적 element.
_TARGET_TAGS: tuple[str, ...] = ("input", "button", "textarea", "select", "label", "a")

# Element + attribute 추출 regex.
# - 셀프 닫는 태그 (`<input ... />`) + 컨테이너 (`<button>text</button>`) 둘 다 매칭.
# - inner text 는 first 100자 + 인접 표현식·중복 공백 제거 후.
_ELEMENT_PATTERN = re.compile(
    r"<(?P<tag>" + "|".join(_TARGET_TAGS) + r")\b(?P<attrs>[^>]*?)"
    r"(?:/>|>(?P<inner>[\s\S]*?)</(?P=tag)>)",
    re.IGNORECASE,
)

# 추가: `<div role="button">...</div>` 같은 ARIA role.
_ROLE_BUTTON_PATTERN = re.compile(
    r'<(?P<tag>\w+)\b(?P<attrs>[^>]*?\brole\s*=\s*["\']button["\'][^>]*?)'
    r"(?:/>|>(?P<inner>[\s\S]*?)</(?P=tag)>)",
    re.IGNORECASE,
)

# attribute 단일 추출 (key="value" 또는 key='value'). v-bind / : prefix 도 인식.
_ATTR_PATTERN = re.compile(
    r'(?:v-bind:|:)?(?P<key>[a-zA-Z][a-zA-Z0-9_:\-]*)\s*=\s*["\'](?P<val>[^"\']*)["\']'
)

# inner text 정제 — Vue 의 `{{ expr }}` 와 JSX 의 `{expr}`, nested HTML/SVG tag, 공백.
_VUE_EXPR_PATTERN = re.compile(r"\{\{[^}]*\}\}")
_JSX_EXPR_PATTERN = re.compile(r"\{[^{}]*\}")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")  # nested element / SVG / break tag 등 제거
_WHITESPACE_PATTERN = re.compile(r"\s+")


class FrontendElement(TypedDict, total=False):
    """단일 frontend element 의 정적 추출 결과.

    runtime DOM 의 fuzzy match 와 동일 schema — UITestTool 옵션 B 의 `_fallback_dom_scan`
    수집 schema 와 align 해 ActionMapper LLM 이 동일 형태로 참조 가능.
    """
    tag: str
    text: str
    placeholder: str
    label: str  # aria-label 또는 인접 <label for=...> 의 text
    testid: str
    name: str
    id: str
    file: str  # 출처 파일 (디버깅용, 상대 경로)


def scan_frontend_directory(project_root: Path) -> list[FrontendElement]:
    """프로젝트 루트의 frontend 디렉토리 후보를 순회하며 의미적 element 추출.

    Returns:
        FrontendElement list. 비어있을 수 있음 (frontend dir 없음 / 파일 없음).
    """
    elements: list[FrontendElement] = []
    seen_files: set[Path] = set()  # 중복 스캔 방지 (frontend/src 같은 중첩 케이스)

    for fe_name in _FE_DIR_CANDIDATES:
        fe_root = project_root / fe_name
        if not fe_root.is_dir():
            continue
        for ext in _FE_EXTENSIONS:
            for fpath in fe_root.rglob(f"*{ext}"):
                if fpath in seen_files:
                    continue
                seen_files.add(fpath)
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = str(fpath.relative_to(project_root))
                elements.extend(_extract_elements_from_text(text, rel))
    return elements


def _extract_elements_from_text(text: str, file_rel: str) -> list[FrontendElement]:
    """단일 파일의 텍스트에서 의미적 element 추출. SFC 의 <template> 부분 만 처리.

    `.vue` 인 경우 `<template>...</template>` 슬라이스. 아닌 경우 (.tsx/.jsx) 전체.
    """
    if "<template" in text.lower():
        m = re.search(r"<template[^>]*>([\s\S]*?)</template>", text, re.IGNORECASE)
        if m:
            text = m.group(1)

    # label 별도 인덱싱 — <label for="X">text</label> 구조로 input/button 의 label 추출.
    label_map = _build_label_map(text)

    found: list[FrontendElement] = []
    for match in _ELEMENT_PATTERN.finditer(text):
        tag = match.group("tag").lower()
        attrs = _parse_attrs(match.group("attrs") or "")
        inner = _clean_inner_text(match.group("inner") or "")

        target_id = attrs.get("id", "")
        label_text = attrs.get("aria-label", "") or (label_map.get(target_id, "") if target_id else "")

        element: FrontendElement = {
            "tag": tag,
            "text": inner[:100] if inner else "",
            "placeholder": attrs.get("placeholder", ""),
            "label": label_text,
            "testid": attrs.get("data-testid") or attrs.get("data-test-id") or "",
            "name": attrs.get("name", ""),
            "id": target_id,
            "file": file_rel,
        }
        if any(element[k] for k in ("text", "placeholder", "label", "testid", "name", "id")):
            found.append(element)

    # role="button" 추가 — _TARGET_TAGS 외 element 도 ARIA button 이면 포함.
    for match in _ROLE_BUTTON_PATTERN.finditer(text):
        tag = match.group("tag").lower()
        if tag in _TARGET_TAGS:
            continue  # 이미 _ELEMENT_PATTERN 에서 처리됨
        attrs = _parse_attrs(match.group("attrs") or "")
        inner = _clean_inner_text(match.group("inner") or "")
        element = {
            "tag": tag,
            "text": inner[:100] if inner else "",
            "placeholder": "",
            "label": attrs.get("aria-label", ""),
            "testid": attrs.get("data-testid") or attrs.get("data-test-id") or "",
            "name": "",
            "id": attrs.get("id", ""),
            "file": file_rel,
        }
        if any(element[k] for k in ("text", "label", "testid", "id")):
            found.append(element)

    return found


def _build_label_map(text: str) -> dict[str, str]:
    """`<label for="email">이메일</label>` 구조 추출 — id → label text 매핑."""
    label_map: dict[str, str] = {}
    for match in re.finditer(r'<label\b[^>]*?\bfor\s*=\s*["\']([^"\']+)["\'][^>]*>([\s\S]*?)</label>',
                              text, re.IGNORECASE):
        target_id = match.group(1).strip()
        label_text = _clean_inner_text(match.group(2))
        if target_id and label_text:
            label_map[target_id] = label_text[:100]
    return label_map


def _parse_attrs(attrs_str: str) -> dict[str, str]:
    """element 의 attribute 문자열 파싱 — `key="value"` 추출. v-bind/: prefix 정규화."""
    result: dict[str, str] = {}
    for match in _ATTR_PATTERN.finditer(attrs_str):
        key = match.group("key").lower()
        val = match.group("val")
        if key not in result:  # 첫 발견 우선
            result[key] = val
    return result


def _clean_inner_text(text: str) -> str:
    """inner text 정제 — Vue `{{ expr }}` / JSX `{expr}` / nested HTML 태그 제거 + 공백 정규화.

    nested <svg>/<i>/<span> 등의 markup 을 제거해 사용자 가시 텍스트만 남긴다.
    """
    text = _VUE_EXPR_PATTERN.sub("", text)
    text = _JSX_EXPR_PATTERN.sub("", text)
    text = _HTML_TAG_PATTERN.sub(" ", text)  # nested tag → 공백 (단어 경계 보존)
    text = _WHITESPACE_PATTERN.sub(" ", text)
    return text.strip()


def write_frontend_index(elements: list[FrontendElement], output_path: Path) -> None:
    """frontend.json 으로 디스크 저장. spec §6.1 의 codebase-index/ 안에 위치."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "element_count": len(elements),
        "elements": elements,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_frontend_index(output_path: Path) -> list[FrontendElement]:
    """frontend.json 로드. 파일 없음/파싱 실패 → 빈 list (graceful)."""
    if not output_path.is_file():
        return []
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    elements = payload.get("elements", [])
    if not isinstance(elements, list):
        return []
    return elements


__all__ = [
    "FrontendElement",
    "scan_frontend_directory",
    "write_frontend_index",
    "load_frontend_index",
]
