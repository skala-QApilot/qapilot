"""vue_sfc_parser 단위 테스트 — PoC 2 (데이터 layer).

fixture: tests/fixtures/vue/signup_minimal.vue (6 testid + dynamic + disabled_when).

본 테스트는 Node.js + @vue/compiler-sfc 가 필요 (qapilot/node-bridge/ 의존).
node 미설치 / 의존성 미설치 환경에서는 skip 한다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from qapilot.scan.extractors.vue_sfc_parser import (
    VueSfcParseError,
    extract_selectors_from_vue,
)
from qapilot.shared.metadata_schemas import (
    ButtonElement,
    DynamicElement,
    InputElement,
    OutputElement,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "vue"
FIXTURE_REPO = FIXTURE_DIR  # 테스트 fixture 의 repo_root 는 fixtures/vue 자체
SIGNUP_FIXTURE = FIXTURE_DIR / "signup_minimal.vue"
NODE_BRIDGE = Path(__file__).resolve().parents[1] / "qapilot" / "node-bridge"
COMMIT_SHA = "f" * 40


def _node_bridge_ready() -> bool:
    if shutil.which("node") is None:
        return False
    if not (NODE_BRIDGE / "node_modules" / "@vue" / "compiler-sfc").exists():
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _node_bridge_ready(),
    reason="node-bridge not installed — run `npm install` in qapilot/node-bridge/",
)


@pytest.fixture
def extracted():
    return extract_selectors_from_vue(
        SIGNUP_FIXTURE, repo_root=FIXTURE_REPO, commit_sha=COMMIT_SHA,
    )


def _by_testid(elements, testid):
    return next(e for e in elements if e.testid == testid)


# ────────────────────────────────────────────────────────────────────────
# 카운트 + 분류
# ────────────────────────────────────────────────────────────────────────

def test_all_testids_extracted(extracted):
    """fixture 의 6 testid 모두 추출 — name/email/guardian-wrap/guardian-consent/error-msg/submit."""
    testids = {e.testid for e in extracted}
    assert testids == {
        "name", "email", "guardian-wrap", "guardian-consent",
        "error-msg", "submit",
    }


def test_classification_by_tag(extracted):
    """tag 별 분류 — input → InputElement, button → ButtonElement, div → OutputElement."""
    assert isinstance(_by_testid(extracted, "name"), InputElement)
    assert isinstance(_by_testid(extracted, "email"), InputElement)
    assert isinstance(_by_testid(extracted, "submit"), ButtonElement)
    assert isinstance(_by_testid(extracted, "error-msg"), OutputElement)


def test_parent_v_if_promotes_to_dynamic(extracted):
    """parent v-if 안의 element 는 DynamicElement 로 재분류 — guardian-consent (input)."""
    el = _by_testid(extracted, "guardian-consent")
    assert isinstance(el, DynamicElement)
    assert el.v_if == "isMinor"
    assert el.html_tag == "input"


def test_self_v_if_keeps_native_type(extracted):
    """element 자체 v-if 만 가지면 native type 유지 + v_if 채움 — guardian-wrap (div)."""
    el = _by_testid(extracted, "guardian-wrap")
    assert isinstance(el, OutputElement)
    assert el.v_if == "isMinor"
    assert el.html_tag == "div"


# ────────────────────────────────────────────────────────────────────────
# 속성 추출
# ────────────────────────────────────────────────────────────────────────

def test_input_v_model_extracted(extracted):
    """v-model expression 추출."""
    el = _by_testid(extracted, "email")
    assert el.v_model == "form.email"


def test_input_html_type_and_validators(extracted):
    """type='email' + required → validators=['required', 'email']."""
    el = _by_testid(extracted, "email")
    assert el.html_type == "email"
    assert el.required is True
    assert "required" in el.validators
    assert "email" in el.validators


def test_input_placeholder(extracted):
    """placeholder 속성 추출."""
    el = _by_testid(extracted, "name")
    assert el.placeholder == "이름"


def test_button_disabled_when(extracted):
    """:disabled (v-bind) 의 expression 추출."""
    el = _by_testid(extracted, "submit")
    assert el.disabled_when is not None
    assert el.disabled_when.expr == "loading || (isMinor && !form.guardian_consent)"
    # PoC 단계 — LLM 의미 라벨링은 후속
    assert el.disabled_when.semantic is None
    assert el.disabled_when.extraction_method_for_semantic is None


def test_button_html_type_submit(extracted):
    """type='submit' 추출."""
    el = _by_testid(extracted, "submit")
    assert el.html_type == "submit"


# ────────────────────────────────────────────────────────────────────────
# 메타 (extracted_from, confidence)
# ────────────────────────────────────────────────────────────────────────

def test_extracted_from_relative_path(extracted):
    """extracted_from.file 은 repo_root 기준 상대 경로."""
    el = _by_testid(extracted, "name")
    # fixtures/vue 가 repo_root 이므로 파일명만 남음
    assert el.extracted_from.file == "signup_minimal.vue"


def test_extracted_from_commit_sha(extracted):
    el = _by_testid(extracted, "name")
    assert el.extracted_from.commit_sha == COMMIT_SHA


def test_extracted_from_line_range(extracted):
    """line_start ≤ line_end + 합리적 line 번호."""
    for el in extracted:
        assert el.extracted_from.line_start >= 1
        assert el.extracted_from.line_end >= el.extracted_from.line_start


def test_confidence_is_one_for_ast(extracted):
    """AST 만으로 추출 = confidence 1.0, method='ast'."""
    for el in extracted:
        assert el.confidence == 1.0
        assert el.extraction_method == "ast"


# ────────────────────────────────────────────────────────────────────────
# 에러 핸들링
# ────────────────────────────────────────────────────────────────────────

def test_missing_file_raises(tmp_path):
    """존재하지 않는 파일 → VueSfcParseError."""
    with pytest.raises(VueSfcParseError):
        extract_selectors_from_vue(
            tmp_path / "no_such_file.vue",
            repo_root=tmp_path, commit_sha=COMMIT_SHA,
        )


def test_no_template_returns_empty(tmp_path):
    """<template> 없는 .vue → 빈 list."""
    f = tmp_path / "no_template.vue"
    f.write_text("<script>export default {}</script>")
    out = extract_selectors_from_vue(f, repo_root=tmp_path, commit_sha=COMMIT_SHA)
    assert out == []


def test_repo_root_outside_falls_back_to_absolute(tmp_path):
    """repo_root 밖 파일 → 절대 경로 fallback (warning + 진행)."""
    f = FIXTURE_DIR / "signup_minimal.vue"
    out = extract_selectors_from_vue(
        f, repo_root=tmp_path,  # tmp_path 와 fixture 는 무관
        commit_sha=COMMIT_SHA,
    )
    # 추출은 정상 진행
    assert len(out) >= 1
    # 단 file 은 절대 경로
    assert out[0].extracted_from.file == str(f.resolve())
