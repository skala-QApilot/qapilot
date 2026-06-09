"""이슈 #221: Qdrant scenario_index 전환 검증.

ScenarioVectorStore의 upsert / search / delete,
pipeline._rough_match_scenarios / _resolve_scenario_targets의 Qdrant 우선·폴백,
POST /api/agent/scenarios/reindex 엔드포인트를 단위 검증한다.

외부 의존(Qdrant, BGE-M3)은 전부 mock으로 격리한다.
- AsyncQdrantClient: 함수 내 lazy import → qdrant_client.AsyncQdrantClient 패치
- ScenarioVectorStore: 사용 위치 기준 qapilot.tools.scenario_index.ScenarioVectorStore 패치
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


# ──────────────────────────────────────────────────────────────────────────────
# 공통 픽스처
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def dummy_ts():
    return {
        "ts_id": "TS-001",
        "title": "사용자 인증 시나리오",
        "test_cases": [
            {"tc_id": "TS-001-TC-01", "name": "정상 로그인"},
            {"tc_id": "TS-001-TC-02", "name": "잘못된 비밀번호 로그인"},
        ],
    }


@pytest.fixture()
def mock_embedder():
    """BGE-M3 인코더 mock — 텍스트 수만큼 1024차원 영벡터 반환."""
    embedder = MagicMock()
    embedder.encode.side_effect = lambda texts, **_: np.zeros((len(texts), 1024), dtype="float32")
    return embedder


@pytest.fixture()
def mock_async_qdrant():
    """AsyncQdrantClient mock."""
    client = AsyncMock()
    client.get_collections.return_value = MagicMock(
        collections=[MagicMock(name="scenario_index")]
    )
    return client


# ──────────────────────────────────────────────────────────────────────────────
# ScenarioVectorStore — upsert_scenario
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_scenario_calls_qdrant_upsert(dummy_ts, mock_embedder, mock_async_qdrant):
    """upsert_scenario: TS 1개 + TC 2개 = 3 포인트로 Qdrant upsert 호출."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qdrant_client.AsyncQdrantClient", return_value=mock_async_qdrant):

        result = await ScenarioVectorStore().upsert_scenario(service_id="svc-1", ts=dummy_ts)

    assert result is True
    mock_async_qdrant.upsert.assert_called_once()
    args, kwargs = mock_async_qdrant.upsert.call_args
    points = kwargs.get("points") or (args[1] if len(args) > 1 else [])
    assert len(points) == 3  # TS 1 + TC 2


@pytest.mark.asyncio
async def test_upsert_scenario_graceful_on_qdrant_error(dummy_ts, mock_embedder):
    """upsert_scenario: Qdrant upsert 장애 시 False 반환 — 예외 전파 없음."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    failing = AsyncMock()
    failing.get_collections.return_value = MagicMock(collections=[MagicMock(name="scenario_index")])
    failing.upsert.side_effect = RuntimeError("Qdrant upsert failed")

    with patch("qapilot.tools.scenario_index._store.get_embedder", return_value=mock_embedder), \
         patch("qdrant_client.AsyncQdrantClient", return_value=failing):

        result = await ScenarioVectorStore().upsert_scenario(service_id="svc-1", ts=dummy_ts)

    assert result is False


@pytest.mark.asyncio
async def test_upsert_scenario_skips_on_empty_service_id(dummy_ts):
    """upsert_scenario: service_id 없으면 즉시 False — Qdrant 미호출."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    result = await ScenarioVectorStore().upsert_scenario(service_id="", ts=dummy_ts)

    assert result is False


# ──────────────────────────────────────────────────────────────────────────────
# ScenarioVectorStore — search_ts
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_ts_returns_hits(mock_async_qdrant):
    """search_ts: Qdrant 결과를 ts_id / title / _similarity 형태로 반환."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    hit = MagicMock()
    hit.score = 0.87
    hit.payload = {"ts_id": "TS-001", "title": "로그인", "level": "ts"}
    mock_async_qdrant.search.return_value = [hit]

    with patch("qdrant_client.AsyncQdrantClient", return_value=mock_async_qdrant):
        results = await ScenarioVectorStore().search_ts(
            service_id="svc-1",
            query_vec=[0.0] * 1024,
            top_n=5,
            threshold=0.40,
        )

    assert len(results) == 1
    assert results[0]["ts_id"] == "TS-001"
    assert results[0]["_similarity"] == pytest.approx(0.87, abs=1e-3)


@pytest.mark.asyncio
async def test_search_ts_empty_on_qdrant_error():
    """search_ts: Qdrant 장애 시 빈 리스트 반환."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    failing = AsyncMock()
    failing.search.side_effect = RuntimeError("timeout")

    with patch("qdrant_client.AsyncQdrantClient", return_value=failing):
        results = await ScenarioVectorStore().search_ts(
            service_id="svc-1", query_vec=[0.0] * 1024
        )

    assert results == []


# ──────────────────────────────────────────────────────────────────────────────
# ScenarioVectorStore — search_tc
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_tc_returns_hits(mock_async_qdrant):
    """search_tc: tc_id / title / ts_id / _similarity 형태로 반환."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    hit = MagicMock()
    hit.score = 0.82
    hit.payload = {
        "tc_id": "TS-001-TC-02",
        "title": "잘못된 비밀번호",
        "ts_id": "TS-001",
        "level": "tc",
    }
    mock_async_qdrant.search.return_value = [hit]

    with patch("qdrant_client.AsyncQdrantClient", return_value=mock_async_qdrant):
        results = await ScenarioVectorStore().search_tc(
            service_id="svc-1",
            ts_id="TS-001",
            query_vec=[0.0] * 1024,
        )

    assert len(results) == 1
    assert results[0]["tc_id"] == "TS-001-TC-02"
    assert results[0]["ts_id"] == "TS-001"


# ──────────────────────────────────────────────────────────────────────────────
# ScenarioVectorStore — delete_scenario
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_scenario_calls_qdrant_delete(mock_async_qdrant):
    """delete_scenario: TS+TC ID로 Qdrant delete 호출."""
    from qapilot.tools.scenario_index._store import ScenarioVectorStore

    with patch("qdrant_client.AsyncQdrantClient", return_value=mock_async_qdrant):
        result = await ScenarioVectorStore().delete_scenario(
            service_id="svc-1",
            ts_id="TS-001",
            tc_ids=["TS-001-TC-01", "TS-001-TC-02"],
        )

    assert result is True
    mock_async_qdrant.delete.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# pipeline._rough_match_scenarios
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def existing_summaries():
    return [
        {"ts_id": "TS-001", "title": "사용자 인증 시나리오", "test_cases": []},
        {"ts_id": "TS-002", "title": "요금제 조회 시나리오", "test_cases": []},
    ]


@pytest.mark.asyncio
async def test_rough_match_uses_qdrant_when_service_id(existing_summaries, mock_embedder):
    """_rough_match_scenarios: service_id 있으면 Qdrant search_ts를 먼저 시도한다."""
    from qapilot.orchestrator.pipeline import _rough_match_scenarios

    mock_store = AsyncMock()
    mock_store.search_ts.return_value = [
        {"ts_id": "TS-001", "title": "사용자 인증 시나리오", "_similarity": 0.91}
    ]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):

        results = await _rough_match_scenarios(
            user_input="로그인 관련 케이스 추가해줘",
            existing_scenarios=existing_summaries,
            service_id="svc-1",
        )

    mock_store.search_ts.assert_called_once()
    assert any(r["ts_id"] == "TS-001" for r in results)


@pytest.mark.asyncio
async def test_rough_match_falls_back_to_inmemory_on_qdrant_error(existing_summaries, mock_embedder):
    """_rough_match_scenarios: Qdrant 장애 시 인메모리 폴백 — 예외 전파 없음."""
    from qapilot.orchestrator.pipeline import _rough_match_scenarios

    mock_store = AsyncMock()
    mock_store.search_ts.side_effect = RuntimeError("Qdrant down")

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):

        results = await _rough_match_scenarios(
            user_input="로그인",
            existing_scenarios=existing_summaries,
            service_id="svc-1",
        )

    assert isinstance(results, list)  # 폴백 후 정상 반환


@pytest.mark.asyncio
async def test_rough_match_skips_qdrant_without_service_id(existing_summaries, mock_embedder):
    """_rough_match_scenarios: service_id 없으면 Qdrant를 건너뛰고 인메모리만 사용."""
    from qapilot.orchestrator.pipeline import _rough_match_scenarios

    mock_store = AsyncMock()

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):

        await _rough_match_scenarios(
            user_input="로그인",
            existing_scenarios=existing_summaries,
            service_id=None,
        )

    mock_store.search_ts.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# pipeline._resolve_scenario_targets
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def update_req():
    return {
        "req_id": "FR-001",
        "action_type": "update",
        "target_level": "tc",
        "domain_area": "인증",
        "content": "잘못된 비밀번호로 로그인 시 오류 메시지 확인",
    }


@pytest.fixture()
def top_candidates():
    return [
        {
            "ts_id": "TS-001",
            "title": "사용자 인증 시나리오",
            "test_cases": [
                {"tc_id": "TS-001-TC-01", "name": "정상 로그인"},
                {"tc_id": "TS-001-TC-02", "name": "잘못된 비밀번호"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_resolve_targets_uses_qdrant_for_ts_and_tc(update_req, top_candidates, mock_embedder):
    """_resolve_scenario_targets: Qdrant로 TS·TC 모두 매칭, target_ts_id/target_tc_id 주입."""
    from qapilot.orchestrator.pipeline import _resolve_scenario_targets

    mock_store = AsyncMock()
    mock_store.search_ts.return_value = [
        {"ts_id": "TS-001", "title": "인증", "_similarity": 0.85}
    ]
    mock_store.search_tc.return_value = [
        {"tc_id": "TS-001-TC-02", "title": "잘못된 비밀번호", "ts_id": "TS-001", "_similarity": 0.80}
    ]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):

        result_reqs = await _resolve_scenario_targets(
            requirements=[update_req],
            existing_scenarios=top_candidates,
            service_id="svc-1",
        )

    mock_store.search_ts.assert_called_once()
    mock_store.search_tc.assert_called_once()
    assert result_reqs[0]["target_ts_id"] == "TS-001"
    assert result_reqs[0]["target_tc_id"] == "TS-001-TC-02"


@pytest.mark.asyncio
async def test_resolve_targets_fallback_on_qdrant_error(update_req, top_candidates, mock_embedder):
    """_resolve_scenario_targets: Qdrant 장애 시 인메모리 폴백 — 예외 전파 없음."""
    from qapilot.orchestrator.pipeline import _resolve_scenario_targets

    mock_store = AsyncMock()
    mock_store.search_ts.side_effect = RuntimeError("Qdrant down")

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):

        result_reqs = await _resolve_scenario_targets(
            requirements=[update_req],
            existing_scenarios=top_candidates,
            service_id="svc-1",
        )

    assert isinstance(result_reqs, list)


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/agent/scenarios/reindex 엔드포인트
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def api_client(monkeypatch):
    """내부 토큰 인증을 bypass한 TestClient."""
    monkeypatch.setenv("QAPILOT_INTERNAL_API_TOKEN", "test-token")
    from fastapi.testclient import TestClient
    from qapilot.api.main import create_app
    return TestClient(create_app())


_AUTH = {"Authorization": "Bearer test-token"}
_REINDEX_URL = "/api/agent/scenarios/reindex"


def test_reindex_endpoint_success(api_client, dummy_ts):
    """POST /api/agent/scenarios/reindex: 정상 — upserted/deleted 카운트 반환."""
    mock_store = AsyncMock()
    mock_store.upsert_scenario.return_value = True
    mock_store.delete_scenario.return_value = True

    with patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):
        resp = api_client.post(
            _REINDEX_URL,
            json={
                "service_id": "svc-1",
                "scenarios": [dummy_ts],
                "deleted_ts_ids": ["TS-OLD"],
            },
            headers=_AUTH,
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["upserted"] == 1
    assert data["deleted"] == 1


def test_reindex_endpoint_missing_service_id(api_client):
    """POST /api/agent/scenarios/reindex: service_id 누락 → 에러 응답 (4xx)."""
    resp = api_client.post(
        _REINDEX_URL,
        json={"scenarios": []},
        headers=_AUTH,
    )

    assert resp.status_code >= 400
    assert resp.json()["success"] is False


def test_reindex_endpoint_empty_body(api_client):
    """POST /api/agent/scenarios/reindex: scenarios 빈 배열 → upserted=0."""
    mock_store = AsyncMock()
    mock_store.delete_scenario.return_value = True

    with patch("qapilot.tools.scenario_index.ScenarioVectorStore", return_value=mock_store):
        resp = api_client.post(
            _REINDEX_URL,
            json={"service_id": "svc-1", "scenarios": [], "deleted_ts_ids": []},
            headers=_AUTH,
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["upserted"] == 0
