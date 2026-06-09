"""llm_pattern_classifier 단위 테스트 — PoC 5.1 (본인 영역).

mock LLMClient 으로 cost 0 검증. 실 LLM 통합은 manual integration test 로 별도.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qapilot.scan.extractors.llm_pattern_classifier import (
    _build_user_prompt,
    _parse_response,
    _update_record,
    classify_unknown_patterns,
)
from qapilot.shared.metadata_schemas import (
    ExtractedFrom,
    TestPatternRecord,
)


def _make_record(testid: str = "test_foo", kind: str = "unknown") -> TestPatternRecord:
    return TestPatternRecord(
        extracted_from=ExtractedFrom(
            file="t.py", line_start=1, line_end=10, commit_sha="a" * 40,
        ),
        confidence=1.0,
        extraction_method="ast",
        pattern_kind=kind,  # type: ignore[arg-type]
        framework="pytest",
        file="t.py",
        line_start=1,
        line_end=10,
        snippet=f"def {testid}(client):\n    pass\n",
        purpose=f"test `{testid}` ({kind})",
    )


def _mock_llm(response_content: str) -> MagicMock:
    """가짜 LLMClient — chat() 만 mock."""
    llm = MagicMock()
    llm.chat = AsyncMock(return_value=MagicMock(content=response_content))
    return llm


# ────────────────────────────────────────────────────────────────────────
# parser
# ────────────────────────────────────────────────────────────────────────

def test_parse_valid_json():
    text = '[{"index": 0, "pattern_kind": "db-seed", "purpose": "create"}]'
    out = _parse_response(text)
    assert out == {0: ("db-seed", "create")}


def test_parse_markdown_wrapped():
    """LLM 이 ```json ... ``` 으로 감싸는 경우 대응."""
    text = '```json\n[{"index": 0, "pattern_kind": "assertion", "purpose": "x"}]\n```'
    out = _parse_response(text)
    assert out == {0: ("assertion", "x")}


def test_parse_invalid_json_returns_empty():
    assert _parse_response("not json at all") == {}


def test_parse_non_list_returns_empty():
    assert _parse_response('{"index": 0}') == {}


def test_parse_skips_malformed_entries():
    text = '[{"index": 0, "pattern_kind": "db-seed", "purpose": "p"},' \
           ' {"foo": "bar"},' \
           ' {"index": "wrong-type", "pattern_kind": "x"},' \
           ' {"index": 2, "pattern_kind": "cleanup"}]'
    out = _parse_response(text)
    assert out == {0: ("db-seed", "p"), 2: ("cleanup", "")}


# ────────────────────────────────────────────────────────────────────────
# user prompt
# ────────────────────────────────────────────────────────────────────────

def test_user_prompt_includes_index_and_snippet():
    recs = [_make_record("test_a"), _make_record("test_b")]
    prompt = _build_user_prompt(recs)
    assert "[0]" in prompt
    assert "[1]" in prompt
    assert "test_a" in prompt
    assert "test_b" in prompt


def test_user_prompt_truncates_long_snippet():
    rec = _make_record()
    rec_long = TestPatternRecord(**{**rec.model_dump(), "snippet": "x" * 2000})
    prompt = _build_user_prompt([rec_long])
    assert "truncated" in prompt
    assert len(prompt) < 2200  # snippet 잘렸음


# ────────────────────────────────────────────────────────────────────────
# _update_record
# ────────────────────────────────────────────────────────────────────────

def test_update_record_sets_confidence_and_method():
    rec = _make_record()
    new = _update_record(rec, "db-seed", "create row")
    assert new.pattern_kind == "db-seed"
    assert new.purpose == "create row"
    assert new.confidence == 0.85
    assert new.extraction_method == "hybrid"
    # 원본 불변
    assert rec.pattern_kind == "unknown"
    assert rec.confidence == 1.0


def test_update_record_preserves_purpose_when_empty():
    rec = _make_record()
    new = _update_record(rec, "db-seed", "")
    assert new.purpose == rec.purpose  # 빈 purpose 면 원본 유지


# ────────────────────────────────────────────────────────────────────────
# classify_unknown_patterns — 통합
# ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_no_llm_returns_input_unchanged():
    recs = [_make_record(kind="unknown"), _make_record(kind="auth-setup")]
    out = await classify_unknown_patterns(recs, llm_client=None)
    assert out == recs
    assert out[0].pattern_kind == "unknown"
    assert out[0].confidence == 1.0


@pytest.mark.asyncio
async def test_classify_only_targets_unknown():
    """fixture / auth-setup / mock 은 LLM 호출 안 함 (보존)."""
    recs = [
        _make_record("test_a", "fixture"),
        _make_record("test_b", "auth-setup"),
        _make_record("test_c", "mock"),
    ]
    llm = _mock_llm("[]")
    out = await classify_unknown_patterns(recs, llm_client=llm)
    # unknown 0개이므로 LLM 호출 안 됨
    llm.chat.assert_not_awaited()
    assert [r.pattern_kind for r in out] == ["fixture", "auth-setup", "mock"]


@pytest.mark.asyncio
async def test_classify_replaces_unknown_with_llm_result():
    recs = [_make_record("test_a", "unknown"), _make_record("test_b", "unknown")]
    response = '[{"index": 0, "pattern_kind": "db-seed", "purpose": "seed"},' \
               ' {"index": 1, "pattern_kind": "assertion", "purpose": "assert"}]'
    llm = _mock_llm(response)

    out = await classify_unknown_patterns(recs, llm_client=llm)

    assert out[0].pattern_kind == "db-seed"
    assert out[0].confidence == 0.85
    assert out[0].extraction_method == "hybrid"
    assert out[1].pattern_kind == "assertion"
    llm.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_skips_unknown_kind_from_llm():
    """LLM 이 'unknown' 반환 시 AST 결과 (unknown + confidence=1.0) 유지."""
    recs = [_make_record(kind="unknown")]
    llm = _mock_llm('[{"index": 0, "pattern_kind": "unknown", "purpose": "p"}]')
    out = await classify_unknown_patterns(recs, llm_client=llm)
    # unknown 유지 — confidence 1.0 (AST 결과 보존)
    assert out[0].pattern_kind == "unknown"
    assert out[0].confidence == 1.0


@pytest.mark.asyncio
async def test_classify_skips_invalid_kind():
    """allowed_kinds 외 응답은 무시 (record 보존)."""
    recs = [_make_record(kind="unknown")]
    llm = _mock_llm('[{"index": 0, "pattern_kind": "WRONG-KIND", "purpose": "p"}]')
    out = await classify_unknown_patterns(recs, llm_client=llm)
    assert out[0].pattern_kind == "unknown"
    assert out[0].confidence == 1.0


@pytest.mark.asyncio
async def test_classify_handles_llm_exception_gracefully():
    """LLM 호출 자체가 raise → 해당 batch 무시, 나머지 record 보존."""
    recs = [_make_record(kind="unknown")]
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=RuntimeError("api down"))
    out = await classify_unknown_patterns(recs, llm_client=llm)
    assert out[0].pattern_kind == "unknown"  # 보존


@pytest.mark.asyncio
async def test_classify_batches_when_over_size():
    """batch_size 보다 많은 unknown 은 여러 호출로 나뉨."""
    recs = [_make_record(f"test_{i}", "unknown") for i in range(25)]
    # 각 batch 가 동일 응답 — index 가 batch-local 임에 주의
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=[
        MagicMock(content='[{"index": 0, "pattern_kind": "db-seed", "purpose": "p"}]'),
        MagicMock(content='[{"index": 0, "pattern_kind": "cleanup", "purpose": "p"}]'),
        MagicMock(content='[{"index": 0, "pattern_kind": "assertion", "purpose": "p"}]'),
    ])

    out = await classify_unknown_patterns(recs, llm_client=llm, batch_size=10)

    # 3 batches: 10 + 10 + 5
    assert llm.chat.await_count == 3
    # 각 batch 의 index 0 = 0, 10, 20 으로 매핑
    assert out[0].pattern_kind == "db-seed"
    assert out[10].pattern_kind == "cleanup"
    assert out[20].pattern_kind == "assertion"
    # 나머지는 unknown 유지
    assert out[1].pattern_kind == "unknown"
    assert out[11].pattern_kind == "unknown"
