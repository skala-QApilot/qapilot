"""이슈 #131 — CrossCheck LLM input 압축 검증.

배경: e2e trace `db24608c` 에서 cross_check 의 LLM 호출이 285K tokens (gpt-4o-mini
128K 한계의 2.2배) 로 context_length_exceeded × 21회 fail. 원인: ui_result + api_trace
+ db_result 의 거대 페이로드 (body / console_log / screenshot path) dump.

해결: `_compact_for_llm` 이 핵심 정보 (정합성 분석) 는 보존하면서 거대 필드 정제.

**부분 검증 가능성** — 본 단위 테스트는 LLM 호출을 mock 으로 차단하고 압축 함수만
검증. e2e 전체 사이클 (50분) 안 돌려도 동작 정확성 완전 보장.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.agents.cross_check_agent import (
    CrossCheckAgent,
    _MAX_API_CALLS,
    _MAX_BODY_CHARS,
    _MAX_CONSOLE_LOG_CHARS,
    _MAX_DB_ROWS_PER_SNAPSHOT,
    _MAX_DB_SNAPSHOTS,
    _MAX_ERROR_CHARS,
    _MAX_LLM_INPUT_CHARS,
    _compact_api_body,
    _compact_ui_error,
    _stringify,
    _truncate_str,
)


def _make_agent() -> CrossCheckAgent:
    agent = CrossCheckAgent.__new__(CrossCheckAgent)
    agent.logger = MagicMock()
    agent.llm = MagicMock()
    agent.llm.chat = AsyncMock()
    agent.prompts = MagicMock()
    agent.prompts.system = MagicMock(return_value="SYSTEM")
    agent.prompts.render = MagicMock(side_effect=lambda **kw: f"USER<{kw['context'][:80]}>")
    agent.with_correction_hint = MagicMock(side_effect=lambda p, e: p)
    return agent


# ── _truncate_str / _stringify ──────────────────────────────────────────────


def test_truncate_str_under_limit_returns_as_is():
    assert _truncate_str("short", 100) == "short"


def test_truncate_str_over_limit_truncates_with_marker():
    s = "a" * 50
    out = _truncate_str(s, 10)
    assert out is not None
    assert out.startswith("a" * 10)
    assert "40 more chars" in out


def test_truncate_str_none_returns_none():
    assert _truncate_str(None, 100) is None


def test_truncate_str_non_string_coerced():
    assert _truncate_str(123, 100) == "123"


def test_stringify_dict_to_json():
    assert _stringify({"a": 1}) == '{"a": 1}'


def test_stringify_none_returns_none():
    assert _stringify(None) is None


def test_stringify_str_passthrough():
    assert _stringify("hello") == "hello"


# ── _compact_ui_result ─────────────────────────────────────────────────────


def test_compact_ui_removes_screenshot_path():
    agent = _make_agent()
    ui = {
        "tc_id": "TC-1", "status": "fail",
        "steps": [
            {"step_no": 1, "action": "fill", "status": "pass", "screenshot_path": "/tmp/big.png", "error": None},
            {"step_no": 2, "action": "click", "status": "fail", "screenshot_path": "/tmp/big2.png",
             "error": "Locator.click: Timeout"},
        ],
    }
    out = agent._compact_ui_result(ui)
    for s in out["steps"]:
        assert "screenshot_path" not in s
    assert out["tc_id"] == "TC-1"


def test_compact_ui_truncates_long_error():
    """assertion 패턴 없는 long error 는 _MAX_ERROR_CHARS 로 cap."""
    agent = _make_agent()
    long_error = "TOOL_UI_LOCATOR_NOT_FOUND: " + "x" * 5000
    ui = {"steps": [{"step_no": 1, "action": "fill", "error": long_error}]}
    out = agent._compact_ui_result(ui)
    err = out["steps"][0]["error"]
    assert len(err) <= _MAX_ERROR_CHARS + 50  # marker 길이 여유
    assert "more chars" in err


def test_compact_ui_keeps_last_5_console_logs():
    agent = _make_agent()
    ui = {"steps": [{
        "step_no": 1, "action": "fill",
        "console_logs": [f"log {i}" for i in range(20)],
    }]}
    out = agent._compact_ui_result(ui)
    logs = out["steps"][0]["console_logs"]
    assert len(logs) == 5
    assert logs == ["log 15", "log 16", "log 17", "log 18", "log 19"]


def test_compact_ui_handles_none_input():
    agent = _make_agent()
    assert agent._compact_ui_result(None) == {}  # type: ignore[arg-type]
    assert agent._compact_ui_result({}) == {"steps": []}


# ── _compact_api_trace ──────────────────────────────────────────────────────


def test_compact_api_removes_headers():
    agent = _make_agent()
    api = {
        "tc_id": "TC-1", "total_calls": 1,
        "calls": [{
            "method": "POST", "url": "/api/login", "status_code": 200,
            "request_headers": {"Authorization": "Bearer ..."},
            "response_headers": {"Content-Type": "application/json"},
            "request_body": '{"email":"a@b.c"}',
            "response_body": '{"token":"xxx"}',
            "latency_ms": 50,
        }],
    }
    out = agent._compact_api_trace(api)
    call = out["calls"][0]
    assert "request_headers" not in call
    assert "response_headers" not in call
    assert call["method"] == "POST"
    assert call["status_code"] == 200


def test_compact_api_truncates_large_body():
    agent = _make_agent()
    big_body = "x" * 5000
    api = {"calls": [{"request_body": big_body, "response_body": big_body, "status_code": 200}]}
    out = agent._compact_api_trace(api)
    call = out["calls"][0]
    assert len(call["request_body"]) <= _MAX_BODY_CHARS + 50
    assert "more chars" in call["request_body"]


def test_compact_api_converts_dict_body_to_json_string():
    """small dict body → JSON 문자열 (변환만, 핵심 필드 추출 트리거 안 함)."""
    agent = _make_agent()
    api = {"calls": [{
        "request_body": {"email": "a@b.c", "password": "secret"},
        "status_code": 200,
    }]}
    out = agent._compact_api_trace(api)
    # 작은 body 라 그대로 dump (key 추출 트리거 X)
    assert out["calls"][0]["request_body"] == '{"email": "a@b.c", "password": "secret"}'


def test_compact_api_slices_to_max_calls_with_failed_priority():
    """calls > _MAX_API_CALLS → 실패 호출 우선 + 나머지 first/last 분할."""
    agent = _make_agent()
    # 100 calls — 5개 실패 (인덱스 50~54), 나머지 정상
    calls = []
    for i in range(100):
        status = 500 if 50 <= i < 55 else 200
        calls.append({"url": f"/api/{i}", "status_code": status})
    api = {"calls": calls}
    out = agent._compact_api_trace(api)

    assert len(out["calls"]) == _MAX_API_CALLS
    assert out["_truncated_calls"] == 100 - _MAX_API_CALLS
    # 실패 5건 모두 보존
    failed_in_out = [c for c in out["calls"] if c["status_code"] == 500]
    assert len(failed_in_out) == 5


def test_compact_api_under_limit_keeps_all():
    """calls <= _MAX_API_CALLS → 모두 보존, _truncated_calls 없음."""
    agent = _make_agent()
    api = {"calls": [{"url": f"/api/{i}", "status_code": 200} for i in range(10)]}
    out = agent._compact_api_trace(api)
    assert len(out["calls"]) == 10
    assert "_truncated_calls" not in out


# ── _compact_db_result ─────────────────────────────────────────────────────


def test_compact_db_slices_to_max_snapshots():
    agent = _make_agent()
    db = {
        "tc_id": "TC-1",
        "snapshots": [{"table": f"t{i}", "rows": []} for i in range(50)],
    }
    out = agent._compact_db_result(db)
    assert len(out["snapshots"]) == _MAX_DB_SNAPSHOTS
    assert out["_truncated_snapshots"] == 50 - _MAX_DB_SNAPSHOTS


def test_compact_db_truncates_large_rows():
    agent = _make_agent()
    db = {"snapshots": [{
        "table": "users",
        "rows": [{"id": i, "name": f"user{i}"} for i in range(50)],
    }]}
    out = agent._compact_db_result(db)
    snap = out["snapshots"][0]
    assert len(snap["rows"]) == _MAX_DB_ROWS_PER_SNAPSHOT
    assert snap["_truncated_rows"] == 50 - _MAX_DB_ROWS_PER_SNAPSHOT


def test_compact_db_handles_empty():
    agent = _make_agent()
    assert agent._compact_db_result({}) == {"snapshots": []}
    assert agent._compact_db_result(None) == {}  # type: ignore[arg-type]


# ── _compact_for_llm 통합 ─────────────────────────────────────────────────


def test_compact_for_llm_returns_three_compacted():
    agent = _make_agent()
    ui, api, db = agent._compact_for_llm(
        {"steps": [{"step_no": 1, "screenshot_path": "/x.png"}]},
        {"calls": [{"url": "/a", "request_headers": {"X": "y"}}]},
        {"snapshots": [{"table": "t"}]},
    )
    assert "screenshot_path" not in ui["steps"][0]
    assert "request_headers" not in api["calls"][0]


# ── _analyze_with_llm — 압축이 LLM 호출 input 에 반영 ──────────────────────


@pytest.mark.asyncio
async def test_analyze_with_llm_uses_compacted_input():
    """LLM 호출 시 user_prompt 의 context 가 압축본 (screenshot_path 등 제거됨)."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=MagicMock(
        content=json.dumps({"error_code": "none", "summary": "", "mismatches": [], "match_score": 1.0})
    ))

    ui = {"steps": [{
        "step_no": 1, "action": "fill",
        "screenshot_path": "/should/not/appear/in/prompt.png",
        "error": "x" * 5000,
    }]}
    api = {"calls": [{
        "url": "/api/login", "status_code": 200,
        "request_headers": {"SECRET": "should-not-leak"},
        "response_body": "y" * 5000,
    }]}
    db = {"snapshots": [{"table": "t1", "rows": []}]}

    await agent._analyze_with_llm(ui, api, db, None)

    # prompts.render 의 context 인자 검증
    rendered_context = agent.prompts.render.call_args.kwargs["context"]
    assert "screenshot_path" not in rendered_context
    assert "should-not-leak" not in rendered_context  # header 제거됨
    assert "more chars" in rendered_context  # error / body 압축됨


@pytest.mark.asyncio
async def test_analyze_with_llm_hard_cuts_when_over_max_input():
    """압축 후에도 _MAX_LLM_INPUT_CHARS 초과 시 hard cut + warning 로그."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=MagicMock(
        content=json.dumps({"error_code": "none", "summary": "", "mismatches": [], "match_score": 1.0})
    ))

    # 인위적으로 큰 ui_result 로 초과 유도 — _MAX_LLM_INPUT_CHARS (120K) 넘기기 위해
    # console_logs 마지막 5개 * _MAX_CONSOLE_LOG_CHARS * step 수
    huge_ui = {"steps": [
        {"step_no": i, "action": "fill",
         "console_logs": ["x" * (_MAX_CONSOLE_LOG_CHARS - 1)] * 5}
        for i in range(200)
    ]}
    await agent._analyze_with_llm(huge_ui, {}, {}, None)

    rendered = agent.prompts.render.call_args.kwargs["context"]
    assert len(rendered) <= _MAX_LLM_INPUT_CHARS + 50
    # warning 발화 확인
    warn_calls = [c for c in agent.logger.warning.call_args_list
                  if c.args and c.args[0] == "cross_check_context_hard_truncated"]
    assert len(warn_calls) == 1


@pytest.mark.asyncio
async def test_analyze_with_llm_preserves_core_semantics():
    """압축이 정합성 분석 핵심 정보 (status_code, url, error_code prefix) 는 보존."""
    agent = _make_agent()
    agent.llm.chat = AsyncMock(return_value=MagicMock(
        content=json.dumps({"error_code": "ui-api-mismatch", "summary": "x",
                            "mismatches": [{"field": "f", "ui_value": "a", "api_value": "b", "db_value": None}],
                            "match_score": 0.5})
    ))

    ui = {"steps": [{"step_no": 1, "action": "fill", "status": "pass"}]}
    api = {"calls": [{"url": "/api/login", "status_code": 400}]}
    db = {"snapshots": []}

    await agent._analyze_with_llm(ui, api, db, None)
    rendered = agent.prompts.render.call_args.kwargs["context"]
    # 핵심 보존
    assert "/api/login" in rendered
    assert "400" in rendered  # status_code
    assert "fill" in rendered  # action

    # 결과 파싱 정상
    mismatches, match_score, error_code, summary = await agent._analyze_with_llm(ui, api, db, None)
    assert error_code == "ui-api-mismatch"
    assert match_score == 0.5
    assert len(mismatches) == 1


# ── 의미 보존 검증 (이슈 #131 v2 — 정확도 위협 차단) ─────────────────────────
#
# 단순 char cap 으로는 정합성 분석의 본질 (api_value / ui_value / db_value) 누락 위험.
# 다음 테스트는 본인이 사용자 검증 후 추가한 의미보존 시나리오:


def test_compact_api_body_extracts_status_from_large_json():
    """response_body 가 _MAX_BODY_CHARS 초과해도 핵심 필드 (status) 보존."""
    # 1500 chars 넘는 JSON 안에 status 가 끝에 위치
    payload = {
        "meta": {"trace_id": "x" * 800, "timestamp": "2026-05-20T00:00:00Z"},
        "request_id": "y" * 800,
        "noise_field_1": "z" * 500,
        "status": "CONFIRMED",
        "amount": 5000,
        "error": None,
    }
    result = _compact_api_body(payload)
    assert result is not None
    # 핵심 필드 100% 보존
    assert "CONFIRMED" in result
    assert "5000" in result or "amount" in result
    # 거대 noise 는 잘림
    assert "x" * 800 not in result


def test_compact_api_body_small_dict_passthrough():
    """_MAX_BODY_CHARS 이하 dict 는 그대로 JSON dump (key 추출 트리거 안 함)."""
    small = {"id": 42, "weird_key": "value"}
    result = _compact_api_body(small)
    assert result == '{"id": 42, "weird_key": "value"}'


def test_compact_api_body_string_json_parsed():
    """str 인 body 도 JSON parse 시 핵심 필드 추출."""
    body_str = json.dumps({
        "meta": {"x": "a" * 1000, "y": "b" * 1000},
        "request_id": "c" * 800,
        "status": "FAIL",
        "code": "USER_NOT_FOUND",
    })
    result = _compact_api_body(body_str)
    assert result is not None
    assert "FAIL" in result
    assert "USER_NOT_FOUND" in result


def test_compact_api_body_non_json_string_char_cap():
    """JSON parse 실패한 str → 단순 char cap."""
    s = "<html>" + "x" * 5000 + "</html>"
    result = _compact_api_body(s)
    assert result is not None
    assert len(result) <= _MAX_BODY_CHARS + 50
    assert "more chars" in result


def test_compact_api_body_none_returns_none():
    assert _compact_api_body(None) is None


def test_compact_api_body_lists_truncated():
    """list 안의 dict items 도 핵심 필드 추출."""
    body = {
        "errors": [
            {"code": f"E-{i}", "message": "x" * 100, "noise": "y" * 1000}
            for i in range(20)
        ],
    }
    result = _compact_api_body(body)
    assert result is not None
    # errors 의 처음 5개 item 의 code 가 보존됨 (의미 보존)
    assert "E-0" in result
    assert "E-4" in result


def test_compact_ui_error_extracts_assertion_pattern():
    """긴 error 안의 expected/got 패턴이 보존됨 (정합성 핵심)."""
    long_error = (
        "TOOL_UI_ASSERTION_FAIL: locator timeout after 30s\n"
        + ("call log padding here " * 200)
        + "expected '가입 완료' got '오류 발생'\n"
        + ("more stack noise " * 200)
    )
    out = _compact_ui_error(long_error)
    assert out is not None
    # 핵심 정합성 정보 (expected vs got) 보존
    assert "가입 완료" in out
    assert "오류 발생" in out
    # error code prefix 보존
    assert "TOOL_UI_ASSERTION_FAIL" in out


def test_compact_ui_error_korean_assertion_pattern():
    """한글 키워드 (기대값/실제) 도 인식."""
    long_error = (
        "TOOL_UI_ASSERTION_FAIL\n"
        + ("noise " * 500)
        + "기대값='로그인 성공' 실제='로그인 실패'\n"
        + ("more noise " * 500)
    )
    out = _compact_ui_error(long_error)
    assert out is not None
    assert "로그인 성공" in out
    assert "로그인 실패" in out


def test_compact_ui_error_short_passthrough():
    """짧은 error 는 그대로."""
    assert _compact_ui_error("TOOL_UI_X: short") == "TOOL_UI_X: short"


def test_compact_ui_error_none_returns_none():
    assert _compact_ui_error(None) is None


def test_compact_ui_error_no_pattern_char_cap():
    """assertion 패턴 없는 긴 error → char cap."""
    long_error = "TOOL_UI_LOCATOR_NOT_FOUND: " + ("x" * 5000)
    out = _compact_ui_error(long_error)
    assert out is not None
    assert len(out) <= _MAX_ERROR_CHARS + 50
    assert "TOOL_UI_LOCATOR_NOT_FOUND" in out


def test_compact_api_trace_preserves_response_status_field_under_compaction():
    """핵심 시나리오: BSS 응답의 status 가 큰 페이로드 뒤에 있어도 LLM input 에 보존."""
    agent = _make_agent()
    huge_meta = "x" * 2000
    api = {"calls": [{
        "method": "POST", "url": "/api/order", "status_code": 200,
        "response_body": {
            "meta": {"trace_id": huge_meta, "timestamp": "now"},
            "data": {"order_id": "O-001", "amount": 5000},
            "status": "CONFIRMED",
        },
    }]}
    out = agent._compact_api_trace(api)
    body = out["calls"][0]["response_body"]
    assert body is not None
    assert "CONFIRMED" in body  # 정합성 핵심 보존
    assert huge_meta not in body  # noise 잘림


def test_compact_ui_result_keeps_step_value_and_expected():
    """step 의 selector/value/expected 는 그대로 (LLM 이 ui_value 분석 시 필요)."""
    agent = _make_agent()
    ui = {"steps": [{
        "step_no": 5, "action": "assert", "selector": "환영합니다",
        "selector_type": "text", "expected": "로그인 성공 메시지 노출",
        "value": None, "status": "fail",
        "error": "TOOL_UI_ASSERT: expected '환영합니다' got '오류'",
    }]}
    out = agent._compact_ui_result(ui)
    step = out["steps"][0]
    # 정합성 분석 핵심 필드 100% 보존
    assert step["selector"] == "환영합니다"
    assert step["selector_type"] == "text"
    assert step["expected"] == "로그인 성공 메시지 노출"
    assert step["status"] == "fail"
    assert "환영합니다" in step["error"]


def test_compact_db_result_preserves_row_columns():
    """DB row 의 컬럼 값은 그대로 (db_value 분석에 필수)."""
    agent = _make_agent()
    db = {"snapshots": [{
        "table": "orders",
        "rows": [{"id": 1, "status": "CONFIRMED", "amount": 5000}],
    }]}
    out = agent._compact_db_result(db)
    snap = out["snapshots"][0]
    assert snap["table"] == "orders"
    assert snap["rows"][0]["status"] == "CONFIRMED"
    assert snap["rows"][0]["amount"] == 5000
