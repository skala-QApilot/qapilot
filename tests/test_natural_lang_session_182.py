"""이슈 #182 — natural_lang 세션/필터링/existing_scenarios 검증."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from qapilot.agents.natural_language_agent import NaturalLanguageAgent
from qapilot.orchestrator.pipeline import (
    _coerce_explicit_scenario_tc_add,
    _coerce_explicit_tc_delete,
    _resolve_scenario_targets,
)
from qapilot.shared.config import QApilotConfig
from qapilot.shared.session_store import (
    get_last_exchange,
    get_last_insufficient_exchange,
    load_session,
    new_session_id,
    save_exchange,
)


# ── session_store 단위 테스트 ──────────────────────────────────────────────────

def test_new_session_id_format():
    sid = new_session_id()
    assert sid.startswith("sess-")
    assert len(sid) == 17  # "sess-" + 12 hex chars


def test_save_and_load_exchange(tmp_path):
    sid = "sess-test001"
    save_exchange(tmp_path, sid, "시나리오 만들어줘", "insufficient", "어떤 기능인지 알려주세요.")

    session = load_session(tmp_path, sid)
    assert session["session_id"] == sid
    assert len(session["exchanges"]) == 1
    assert session["exchanges"][0]["user"] == "시나리오 만들어줘"
    assert session["exchanges"][0]["query_status"] == "insufficient"
    assert session["exchanges"][0]["query_feedback"] == "어떤 기능인지 알려주세요."


def test_get_last_insufficient_exchange_returns_none_if_sufficient(tmp_path):
    sid = "sess-test002"
    save_exchange(tmp_path, sid, "결제 시나리오 추가해줘", "sufficient")

    result = get_last_insufficient_exchange(tmp_path, sid)
    assert result is None


def test_get_last_insufficient_exchange_returns_exchange_if_insufficient(tmp_path):
    sid = "sess-test003"
    save_exchange(tmp_path, sid, "시나리오 만들어줘", "insufficient", "어떤 기능인지 알려주세요.")

    result = get_last_insufficient_exchange(tmp_path, sid)
    assert result is not None
    assert result["query_status"] == "insufficient"


def test_get_last_insufficient_only_checks_last_exchange(tmp_path):
    """직전 교환만 확인 — 이전에 insufficient가 있었어도 마지막이 sufficient면 None."""
    sid = "sess-test004"
    save_exchange(tmp_path, sid, "시나리오 만들어줘", "insufficient", "어떤 기능인지 알려주세요.")
    save_exchange(tmp_path, sid, "결제 시나리오 추가해줘", "sufficient")

    result = get_last_insufficient_exchange(tmp_path, sid)
    assert result is None


def test_load_session_returns_empty_if_not_exists(tmp_path):
    session = load_session(tmp_path, "sess-nonexistent")
    assert session["exchanges"] == []


# ── NaturalLanguageAgent._parse_response 단위 테스트 ──────────────────────────

@pytest.fixture
def agent():
    return NaturalLanguageAgent(config=QApilotConfig(), trace_id="test-trace")


def test_parse_response_sufficient(agent):
    content = json.dumps({
        "query_status": "sufficient",
        "requirements": [
            {
                "req_id": "REQ-001",
                "req_type": "functional",
                "content": "사용자는 이메일로 로그인할 수 있다.",
                "priority": "high",
                "domain_area": "인증",
                "action_type": "create",
                "target_level": "ts",
                "target_ts_id": None,
                "target_tc_id": None,
            }
        ],
    })
    result = agent._parse_response(content)
    assert result["query_status"] == "sufficient"
    assert len(result["requirements"]) == 1
    assert result["requirements"][0]["action_type"] == "create"


def test_parse_response_insufficient(agent):
    content = json.dumps({
        "query_status": "insufficient",
        "query_feedback": "어떤 기능 영역인지 알려주세요.",
    })
    result = agent._parse_response(content)
    assert result["query_status"] == "insufficient"
    assert result["query_feedback"] == "어떤 기능 영역인지 알려주세요."
    assert result["requirements"] == []


def test_parse_response_rejected(agent):
    content = json.dumps({
        "query_status": "rejected",
        "query_feedback": "시나리오 생성과 무관한 질문입니다.",
    })
    result = agent._parse_response(content)
    assert result["query_status"] == "rejected"
    assert result["requirements"] == []


def test_build_history_text_empty(agent):
    assert agent._build_history_text([]) == "없음"


def test_build_history_text_with_exchange(agent):
    history = [{"user": "시나리오 만들어줘", "query_feedback": "어떤 기능인지 알려주세요."}]
    text = agent._build_history_text(history)
    assert "시나리오 만들어줘" in text
    assert "어떤 기능인지 알려주세요." in text


def test_build_top_candidates_text_empty(agent):
    assert agent._build_top_candidates_text([]) == "없음"


def test_build_top_candidates_text_with_data(agent):
    candidates = [
        {
            "ts_id": "TS-001",
            "title": "로그인 시나리오",
            "_similarity": 0.82,
            "test_cases": [
                {"tc_id": "TC-001", "title": "정상 로그인"},
                {"tc_id": "TC-002", "title": "비밀번호 오류"},
            ],
        }
    ]
    text = agent._build_top_candidates_text(candidates)
    assert "TS-001" in text
    assert "TC-001" in text
    assert "TC-002" in text
    assert "0.82" in text


def test_validate_requirement_sets_default_target_level(agent):
    item = {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "사용자는 로그인할 수 있다.",
        "priority": "high",
        "domain_area": "인증",
    }
    result = agent._validate_requirement(item)
    assert result["action_type"] == "create"
    assert result["target_level"] == "ts"
    assert result["target_ts_id"] is None
    assert result["target_tc_id"] is None


def test_validate_requirement_update_with_tc_level(agent):
    """target_ts_id / target_tc_id는 LLM 출력에 있어도 항상 None.
    임베딩 매칭 레이어(_resolve_scenario_targets)가 주입한다."""
    item = {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "네이버 이메일로 가입 가능.",
        "priority": "medium",
        "domain_area": "회원가입",
        "action_type": "update",
        "target_level": "tc",
        "target_ts_id": "TS-002",   # LLM 출력값이지만 무시됨
        "target_tc_id": "TC-005",   # LLM 출력값이지만 무시됨
    }
    result = agent._validate_requirement(item)
    assert result["action_type"] == "update"
    assert result["target_level"] == "tc"
    assert result["target_ts_id"] is None   # 매칭 레이어에서 주입
    assert result["target_tc_id"] is None   # 매칭 레이어에서 주입


def test_validate_requirement_preserves_delete_action(agent):
    """LLM이 delete를 반환하면 validate 단계에서 create로 되돌리지 않는다."""
    item = {
        "req_id": "REQ-001",
        "req_type": "functional",
        "content": "청소년 요금제에 약정 가입 시도하는 테스트 케이스를 삭제한다.",
        "priority": "medium",
        "domain_area": "신규 가입",
        "action_type": "delete",
        "target_level": "tc",
    }
    result = agent._validate_requirement(item)

    assert result["action_type"] == "delete"
    assert result["target_level"] == "tc"


# ── _resolve_scenario_targets 임베딩 매칭 단위 테스트 ────────────────────────────

_MOCK_SCENARIOS = [
    {
        "ts_id": "TS-001",
        "title": "로그인 정상 케이스",
        "test_cases": [
            {"tc_id": "TC-001", "title": "이메일 비밀번호 정상 로그인"},
            {"tc_id": "TC-002", "title": "잘못된 비밀번호 오류"},
        ],
    },
    {
        "ts_id": "TS-002",
        "title": "회원가입 플로우",
        "test_cases": [
            {"tc_id": "TC-003", "title": "정상 회원가입"},
            {"tc_id": "TC-004", "title": "중복 이메일 오류"},
        ],
    },
]


def test_coerce_explicit_scenario_tc_add_overrides_create_ts():
    """'기존 TS명에 케이스 추가'는 LLM이 create+ts로 오분류해도 create+tc가 되어야 한다."""
    requirements = [
        {
            "req_id": "REQ-001",
            "content": "순차적 회선 해지 요청 케이스를 추가한다.",
            "domain_area": "회선 해지",
            "action_type": "create",
            "target_level": "ts",
            "target_ts_id": None,
            "target_tc_id": None,
        }
    ]
    scenarios = [
        {"ts_id": "TS-010", "title": "회선 해지 시나리오", "test_cases": []},
        {"ts_id": "TS-011", "title": "회선 가입 시나리오", "test_cases": []},
    ]

    _coerce_explicit_scenario_tc_add(
        "회선 해지 시나리오에 순차적 회선 해지 요청 케이스 추가해줘",
        requirements,
        scenarios,
    )

    assert requirements[0]["action_type"] == "create"
    assert requirements[0]["target_level"] == "tc"
    assert requirements[0]["target_ts_id"] == "TS-010"
    assert requirements[0]["target_tc_id"] is None


def test_coerce_explicit_scenario_tc_add_matches_without_spacing():
    """사용자 입력과 TS 제목의 공백 차이가 있어도 명시적 TS명을 우선한다."""
    requirements = [
        {
            "req_id": "REQ-001",
            "content": "순차적 회선 해지 요청 케이스를 추가한다.",
            "domain_area": "회선해지",
            "action_type": "create",
            "target_level": "ts",
            "target_ts_id": None,
            "target_tc_id": None,
        }
    ]
    scenarios = [
        {"ts_id": "TS-010", "title": "회선 해지 시나리오", "test_cases": []},
    ]

    _coerce_explicit_scenario_tc_add(
        "회선해지시나리오에 순차적 회선 해지 요청 TC 생성",
        requirements,
        scenarios,
    )

    assert requirements[0]["target_level"] == "tc"
    assert requirements[0]["target_ts_id"] == "TS-010"


def test_coerce_explicit_tc_delete_by_full_tc_id():
    requirements = [
        {
            "req_id": "REQ-001",
            "content": "TS-005-TC-04를 삭제한다.",
            "domain_area": "테스트",
            "action_type": "create",
            "target_level": "ts",
            "target_ts_id": None,
            "target_tc_id": None,
        }
    ]

    _coerce_explicit_tc_delete("TS-005-TC-04 삭제", requirements, [])

    assert requirements[0]["action_type"] == "delete"
    assert requirements[0]["target_level"] == "tc"
    assert requirements[0]["target_ts_id"] == "TS-005"
    assert requirements[0]["target_tc_id"] == "TS-005-TC-04"


def test_coerce_explicit_tc_delete_uses_named_scenario_scope():
    requirements = [
        {
            "req_id": "REQ-001",
            "content": "청소년 요금제에 약정 가입 시도하는 테스트 케이스를 삭제한다.",
            "domain_area": "요금제 변경",
            "action_type": "create",
            "target_level": "ts",
            "target_ts_id": None,
            "target_tc_id": None,
        }
    ]
    scenarios = [
        {
            "ts_id": "TS-005",
            "title": "신규 가입 시나리오",
            "test_cases": [{"tc_id": "TS-005-TC-04", "title": "청소년 요금제에 약정 가입 시도"}],
        }
    ]

    _coerce_explicit_tc_delete(
        "신규 가입 시나리오에서 청소년 요금제에 약정 가입 시도하는 테스트 케이스 삭제",
        requirements,
        scenarios,
    )

    assert requirements[0]["action_type"] == "delete"
    assert requirements[0]["target_level"] == "tc"
    assert requirements[0]["target_ts_id"] == "TS-005"
    assert requirements[0]["target_tc_id"] is None


def _make_vec(seed: int, dim: int = 8) -> np.ndarray:
    """재현 가능한 단위 벡터를 만든다."""
    rng = np.random.default_rng(seed)
    v = rng.random(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_embedder_mock(query_vec: np.ndarray, ts_vecs: list, tc_vecs: list | None = None):
    """encode 호출 순서에 따라 적절한 벡터 배열을 반환하는 mock."""
    # pipeline 호출 순서: query_texts → ts_texts → (tc_texts)
    call_count = [0]
    all_returns = [query_vec.reshape(1, -1), np.array(ts_vecs)]
    if tc_vecs is not None:
        all_returns.append(np.array(tc_vecs))

    class _MockEmbedder:
        def encode(self, texts, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return all_returns[idx % len(all_returns)]

    return _MockEmbedder()


@pytest.mark.asyncio
async def test_resolve_targets_injects_ts_id_on_high_similarity():
    """유사도 threshold 이상이면 target_ts_id가 주입된다."""
    # 회원가입 쿼리 벡터와 TS-002 벡터를 거의 동일하게 설정
    q_vec = _make_vec(42)
    ts_vecs = [_make_vec(99), q_vec]  # TS-001은 낮음, TS-002는 거의 동일

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=_make_embedder_mock(q_vec, ts_vecs)):
        requirements = [
            {
                "req_id": "REQ-001",
                "content": "사용자는 네이버 도메인 이메일로 회원가입할 수 있다.",
                "domain_area": "회원가입",
                "action_type": "update",
                "target_level": "ts",
                "target_ts_id": None,
                "target_tc_id": None,
            }
        ]
        result = await _resolve_scenario_targets(requirements, _MOCK_SCENARIOS, threshold=0.5)

    assert result[0]["target_ts_id"] == "TS-002"


@pytest.mark.asyncio
async def test_resolve_targets_skips_create_action():
    """action_type: create인 항목은 target_ts_id를 주입하지 않는다."""
    q_vec = _make_vec(42)
    ts_vecs = [q_vec, _make_vec(99)]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=_make_embedder_mock(q_vec, ts_vecs)):
        requirements = [
            {
                "req_id": "REQ-001",
                "content": "새 시나리오를 만든다.",
                "domain_area": "로그인",
                "action_type": "create",
                "target_level": "ts",
                "target_ts_id": None,
                "target_tc_id": None,
            }
        ]
        result = await _resolve_scenario_targets(requirements, _MOCK_SCENARIOS, threshold=0.5)

    # create는 매칭 시도 자체를 하지 않음
    assert result[0]["target_ts_id"] is None


@pytest.mark.asyncio
async def test_resolve_targets_null_on_low_similarity():
    """유사도 threshold 미달이면 target_ts_id가 null로 유지된다."""
    q_vec = _make_vec(42)
    # 모든 TS 벡터를 직교에 가깝게 설정
    ts_vecs = [_make_vec(1), _make_vec(2)]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=_make_embedder_mock(q_vec, ts_vecs)):
        requirements = [
            {
                "req_id": "REQ-001",
                "content": "사용자는 결제할 수 있다.",
                "domain_area": "결제",
                "action_type": "update",
                "target_level": "ts",
                "target_ts_id": None,
                "target_tc_id": None,
            }
        ]
        result = await _resolve_scenario_targets(requirements, _MOCK_SCENARIOS, threshold=0.99)

    assert result[0]["target_ts_id"] is None


@pytest.mark.asyncio
async def test_resolve_targets_injects_tc_id_on_tc_level():
    """target_level: tc이면 TS 매칭 후 TC까지 매칭한다."""
    q_vec = _make_vec(42)
    ts_vecs = [_make_vec(99), q_vec]           # TS-002 매칭
    tc_vecs = [q_vec, _make_vec(99)]           # TC-003 매칭

    with patch(
        "qapilot.tools.domain_knowledge._embedder.get_embedder",
        return_value=_make_embedder_mock(q_vec, ts_vecs, tc_vecs),
    ):
        requirements = [
            {
                "req_id": "REQ-001",
                "content": "사용자는 정상적으로 회원가입할 수 있다.",
                "domain_area": "회원가입",
                "action_type": "update",
                "target_level": "tc",
                "target_ts_id": None,
                "target_tc_id": None,
            }
        ]
        result = await _resolve_scenario_targets(requirements, _MOCK_SCENARIOS, threshold=0.5)

    assert result[0]["target_ts_id"] == "TS-002"
    assert result[0]["target_tc_id"] == "TC-003"


def test_get_last_exchange_returns_successful_exchange(tmp_path):
    """get_last_exchange는 sufficient 교환도 반환한다."""
    sid = "sess-test010"
    save_exchange(tmp_path, sid, "결제 시나리오 추가해줘", "sufficient")

    result = get_last_exchange(tmp_path, sid)
    assert result is not None
    assert result["query_status"] == "sufficient"


def test_get_last_exchange_returns_none_on_rejected(tmp_path):
    """get_last_exchange는 rejected 교환은 None 반환 (맥락 오염 방지)."""
    sid = "sess-test011"
    save_exchange(tmp_path, sid, "오늘 날씨 어때", "rejected", "관련 없는 질문입니다.")

    result = get_last_exchange(tmp_path, sid)
    assert result is None
