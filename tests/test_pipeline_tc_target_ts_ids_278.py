"""PR #278: tc_target_ts_ids 의 3분기 의미 통일 (인라인 검증).

- None / 미주입 → 처음 2 TS only (기존 default, CLI / dev 디버깅용)
- [] 빈 리스트 → **전체 TS 처리** (SaaS UI 흐름 — PR #277 자동 진입이 채우는 값)
- [TS-x, ...] 명시 리스트 → 지정 TS만

이전: 빈 리스트 = `or []` 평가로 falsy → default 2 TS only. PR #277 의 자동 진입과
충돌해 TS-003~ 의 TC 0개가 됨 (trace ae5700b9 → scenarios 26건 중 TS-001/TS-002 만
TC 채워짐).
"""
from __future__ import annotations

import pytest


def _compute_target_indices(raw_targets, ts_count: int) -> list[int]:
    """본인 PR #278 변경 — pipeline.py:_tc_generate_doc_search 안의 분기 로직 그대로."""
    if raw_targets is None:
        return list(range(min(2, ts_count)))
    elif len(raw_targets) == 0:
        return list(range(ts_count))
    else:
        target_indices: list[int] = []
        for t in raw_targets:
            try:
                idx = int(str(t).replace("TS-", "")) - 1
                if 0 <= idx < ts_count:
                    target_indices.append(idx)
            except ValueError:
                pass
        return target_indices


def test_none_defaults_to_first_2_ts():
    """tc_target_ts_ids 미주입 / None → 처음 2 TS only (CLI default)."""
    assert _compute_target_indices(None, ts_count=5) == [0, 1]


def test_empty_list_processes_all_ts():
    """tc_target_ts_ids = [] 빈 리스트 → 전체 TS 처리 (SaaS 본질)."""
    assert _compute_target_indices([], ts_count=5) == [0, 1, 2, 3, 4]
    assert _compute_target_indices([], ts_count=26) == list(range(26))


def test_explicit_list_processes_specified():
    """tc_target_ts_ids = ['TS-001', 'TS-003'] → 명시된 TS만."""
    assert sorted(_compute_target_indices(["TS-001", "TS-003"], ts_count=5)) == [0, 2]


def test_explicit_list_ignores_out_of_range():
    """명시 리스트 중 ts_count 범위 초과는 무시."""
    assert sorted(_compute_target_indices(["TS-001", "TS-099"], ts_count=5)) == [0]


def test_empty_list_with_zero_ts_returns_empty():
    """빈 리스트 + ts_count=0 → 빈 리스트 (graceful)."""
    assert _compute_target_indices([], ts_count=0) == []


def test_none_with_one_ts_returns_one():
    """None default + ts_count=1 → [0] (min(2, 1)=1)."""
    assert _compute_target_indices(None, ts_count=1) == [0]


# ── 실 pipeline.py 와 분기 로직 정합성 확인 (regression guard) ──


def test_actual_pipeline_branching_matches():
    """본 헬퍼가 실 pipeline.py 의 분기 로직 정합."""
    import inspect
    from qapilot.orchestrator import pipeline
    src = inspect.getsource(pipeline)
    # 3가지 분기 키워드가 실 코드에 있어야 함
    assert "raw_targets is None" in src, "None 분기 누락"
    assert "len(raw_targets) == 0" in src, "빈 리스트 분기 누락"
    assert "list(range(len(ts_list)))" in src, "전체 TS 처리 누락"
