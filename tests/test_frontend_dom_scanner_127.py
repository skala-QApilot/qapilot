"""이슈 #127 — frontend_dom_scanner 정확성 검증.

regex 휴리스틱으로 Vue SFC / TSX / JSX 의 의미적 element + attribute 추출.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qapilot.tools.frontend_dom_scanner import (
    FrontendElement,
    _build_label_map,
    _clean_inner_text,
    _extract_elements_from_text,
    _parse_attrs,
    load_frontend_index,
    scan_frontend_directory,
    write_frontend_index,
)


# ── _parse_attrs / _clean_inner_text / _build_label_map ─────────────────────


def test_parse_attrs_basic():
    attrs = _parse_attrs('placeholder="이메일" data-testid="email" name="email"')
    assert attrs == {"placeholder": "이메일", "data-testid": "email", "name": "email"}


def test_parse_attrs_single_quote_and_v_bind():
    """단일 따옴표 + Vue 의 v-bind / : prefix 정규화."""
    attrs = _parse_attrs(":placeholder='이메일' v-bind:data-testid=\"email\"")
    assert attrs.get("placeholder") == "이메일"
    assert attrs.get("data-testid") == "email"


def test_clean_inner_text_removes_vue_and_jsx_expr():
    assert _clean_inner_text("로그인 {{ status }} 완료") == "로그인 완료"
    assert _clean_inner_text("결과: {value}") == "결과:"
    assert _clean_inner_text("  여러   공백   ") == "여러 공백"


def test_clean_inner_text_strips_nested_html_tags():
    """nested <svg>/<i>/<span> 등의 markup 제거 — 사용자 가시 텍스트만 남김."""
    assert _clean_inner_text('<svg width="14"><path d="M0..."/></svg> 메뉴') == "메뉴"
    assert _clean_inner_text("<i class='icon'></i>로그아웃") == "로그아웃"
    assert _clean_inner_text("<br/>줄바꿈<br>다음") == "줄바꿈 다음"


def test_build_label_map_extracts_for_attribute():
    text = '<label for="email">이메일</label><label for="password">비밀번호</label>'
    m = _build_label_map(text)
    assert m == {"email": "이메일", "password": "비밀번호"}


# ── _extract_elements_from_text — Vue SFC ───────────────────────────────────


VUE_LOGIN = """
<template>
  <form>
    <label for="email">이메일</label>
    <input
      v-model="form.email"
      placeholder="example@email.com"
      data-testid="email"
      id="email"
    />
    <label for="password">비밀번호</label>
    <input
      type="password"
      placeholder="비밀번호 입력"
      data-testid="password"
      id="password"
    />
    <button type="submit" data-testid="login-submit">로그인</button>
  </form>
</template>
<script setup>
import { ref } from 'vue'
</script>
"""


def test_extract_from_vue_login_template():
    elements = _extract_elements_from_text(VUE_LOGIN, "frontend/pages/Login.vue")

    # input email
    email = next((e for e in elements if e.get("id") == "email"), None)
    assert email is not None
    assert email["tag"] == "input"
    assert email["placeholder"] == "example@email.com"
    assert email["testid"] == "email"
    assert email["label"] == "이메일"  # <label for="email"> 연결
    assert email["file"] == "frontend/pages/Login.vue"

    # input password
    pwd = next((e for e in elements if e.get("id") == "password"), None)
    assert pwd is not None
    assert pwd["placeholder"] == "비밀번호 입력"
    assert pwd["label"] == "비밀번호"

    # button login-submit — text="로그인"
    btn = next((e for e in elements if e.get("testid") == "login-submit"), None)
    assert btn is not None
    assert btn["tag"] == "button"
    assert btn["text"] == "로그인"


def test_extract_ignores_script_section_in_vue():
    """Vue 의 <script> 부분은 제외 — <template> 만 처리."""
    text = """
<template>
  <button data-testid="submit">제출</button>
</template>
<script>
const fake = '<button data-testid="hidden">숨김</button>'
</script>
"""
    elements = _extract_elements_from_text(text, "x.vue")
    testids = [e.get("testid") for e in elements]
    assert "submit" in testids
    assert "hidden" not in testids  # script 안의 fake 는 무시


# ── TSX / JSX ────────────────────────────────────────────────────────────────


TSX_SIGNUP = """
import React from 'react'
export function SignupForm() {
  return (
    <form>
      <label htmlFor="username">사용자명</label>
      <input
        placeholder="아이디를 입력하세요"
        data-testid="username-input"
        id="username"
        name="username"
      />
      <button type="submit" data-testid="submit-btn">가입</button>
    </form>
  )
}
"""


def test_extract_from_tsx_jsx():
    elements = _extract_elements_from_text(TSX_SIGNUP, "frontend/pages/Signup.tsx")
    # JSX 의 htmlFor 는 label_map 에서 인식 안 됨 (HTML for 와 다름) — 그러나 input 의
    # placeholder/testid 는 정상 추출
    user = next((e for e in elements if e.get("testid") == "username-input"), None)
    assert user is not None
    assert user["placeholder"] == "아이디를 입력하세요"
    assert user["name"] == "username"

    btn = next((e for e in elements if e.get("testid") == "submit-btn"), None)
    assert btn is not None
    assert btn["text"] == "가입"


# ── role="button" 휴리스틱 ──────────────────────────────────────────────────


def test_extract_role_button_on_non_button_tag():
    """`<div role="button">...</div>` 같은 ARIA button 도 추출."""
    text = '<div role="button" data-testid="custom-btn" aria-label="옵션">⚙️</div>'
    elements = _extract_elements_from_text(text, "ui/Custom.tsx")
    e = next((x for x in elements if x.get("testid") == "custom-btn"), None)
    assert e is not None
    assert e["tag"] == "div"
    assert e["label"] == "옵션"


# ── scan_frontend_directory + write/load ───────────────────────────────────


def test_scan_frontend_directory_full_flow(tmp_path: Path):
    """frontend/ 디렉토리 내 .vue 파일 스캔 + 저장 + 로드 종합."""
    (tmp_path / "frontend" / "pages").mkdir(parents=True)
    (tmp_path / "frontend" / "pages" / "Login.vue").write_text(VUE_LOGIN, encoding="utf-8")
    (tmp_path / "frontend" / "pages" / "Signup.tsx").write_text(TSX_SIGNUP, encoding="utf-8")

    elements = scan_frontend_directory(tmp_path)
    assert len(elements) >= 5  # Login.vue 3건 + Signup.tsx 2건

    # 디스크 저장 + 로드
    out = tmp_path / ".qapilot" / "codebase-index" / "frontend.json"
    write_frontend_index(elements, out)
    assert out.is_file()

    loaded = load_frontend_index(out)
    assert len(loaded) == len(elements)
    assert loaded[0]["tag"] in {"input", "button", "label"}


def test_scan_returns_empty_for_no_frontend_dir(tmp_path: Path):
    """frontend 디렉토리 후보 모두 없음 → 빈 list."""
    elements = scan_frontend_directory(tmp_path)
    assert elements == []


def test_scan_explores_multiple_dir_candidates(tmp_path: Path):
    """frontend / web / client 등 5종 디렉토리 후보 모두 탐색."""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "App.tsx").write_text(
        '<button data-testid="ok">확인</button>', encoding="utf-8"
    )
    elements = scan_frontend_directory(tmp_path)
    assert any(e.get("testid") == "ok" for e in elements)


def test_scan_skips_duplicate_files_in_overlap(tmp_path: Path):
    """frontend/src 같은 중첩 디렉토리에서 중복 스캔 방지."""
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "App.tsx").write_text(
        '<input data-testid="dup" />', encoding="utf-8"
    )
    # frontend 와 src 둘 다 후보 — frontend 가 먼저 스캔되면 frontend/src/App.tsx 발견,
    # src 단독으로 또 스캔하면 중복. seen_files 가 중복 방지.
    elements = scan_frontend_directory(tmp_path)
    dup_elements = [e for e in elements if e.get("testid") == "dup"]
    assert len(dup_elements) == 1


def test_load_returns_empty_when_file_missing(tmp_path: Path):
    """frontend.json 부재 → 빈 list."""
    assert load_frontend_index(tmp_path / "missing.json") == []


def test_load_returns_empty_for_invalid_json(tmp_path: Path):
    """JSON parse 실패 → 빈 list (graceful)."""
    f = tmp_path / "broken.json"
    f.write_text("not valid json {{{", encoding="utf-8")
    assert load_frontend_index(f) == []


def test_write_creates_parent_directory(tmp_path: Path):
    """codebase-index/ 디렉토리 자동 생성."""
    out = tmp_path / "deep" / "nested" / "frontend.json"
    write_frontend_index([{"tag": "input", "text": "", "file": "x"}], out)
    assert out.is_file()
    data = json.loads(out.read_text())
    assert data["element_count"] == 1
    assert data["elements"][0]["tag"] == "input"


# ── element 가 모든 식별자 비어있을 때 제외 ──────────────────────────────────


def test_extract_skips_meaningless_element():
    """text/placeholder/label/testid/name/id 모두 빈 element 는 제외."""
    text = "<input /><input placeholder='ok' />"
    elements = _extract_elements_from_text(text, "x.vue")
    # 첫 input (모두 빈) 은 제외, 두 번째 (placeholder='ok') 만 포함
    assert len(elements) == 1
    assert elements[0]["placeholder"] == "ok"
