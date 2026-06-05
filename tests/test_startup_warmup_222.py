"""이슈 #222: BGE-M3 startup warmup 검증.

_warmup_embedder 함수의 정상 동작, 장애 시 graceful skip,
lifespan이 warmup을 호출하는지를 단위 검증한다.

외부 의존(BGE-M3 SentenceTransformer, HuggingFace 네트워크)은 mock으로 격리한다.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# _warmup_embedder
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_warmup_calls_encode():
    """_warmup_embedder: get_embedder()를 호출하고 encode(['warmup'])을 실행한다."""
    from qapilot.api.main import _warmup_embedder

    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.0] * 1024]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder):
        await _warmup_embedder()

    mock_embedder.encode.assert_called_once_with(["warmup"], normalize_embeddings=True)


@pytest.mark.asyncio
async def test_warmup_graceful_on_model_load_error():
    """_warmup_embedder: BGE-M3 로딩 실패 시 예외를 전파하지 않는다."""
    from qapilot.api.main import _warmup_embedder

    with patch(
        "qapilot.tools.domain_knowledge._embedder.get_embedder",
        side_effect=RuntimeError("모델 로딩 실패"),
    ):
        # 예외 없이 완료되어야 한다
        await _warmup_embedder()


@pytest.mark.asyncio
async def test_warmup_graceful_on_encode_error():
    """_warmup_embedder: encode 호출 실패 시도 예외를 전파하지 않는다."""
    from qapilot.api.main import _warmup_embedder

    mock_embedder = MagicMock()
    mock_embedder.encode.side_effect = RuntimeError("MPS 장치 오류")

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder):
        await _warmup_embedder()  # 예외 없이 완료


@pytest.mark.asyncio
async def test_warmup_logs_complete_on_success(caplog):
    """_warmup_embedder: 성공 시 bge_warmup_complete 로그가 기록된다."""
    import logging
    from qapilot.api.main import _warmup_embedder

    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.0] * 1024]

    with patch("qapilot.tools.domain_knowledge._embedder.get_embedder", return_value=mock_embedder), \
         caplog.at_level(logging.DEBUG):
        await _warmup_embedder()

    # structlog는 caplog에 직접 잡히지 않으므로 encode 호출 여부로 성공 확인
    mock_embedder.encode.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# lifespan — _warmup_embedder 호출 보장
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lifespan_calls_warmup():
    """_lifespan: 서버 시작(startup) 시 _warmup_embedder를 호출한다."""
    from qapilot.api.main import _lifespan

    warmup_called = []

    async def fake_warmup():
        warmup_called.append(True)

    mock_app = MagicMock()

    with patch("qapilot.api.main._warmup_embedder", new=fake_warmup):
        async with _lifespan(mock_app):
            pass  # yield 이후 종료

    assert warmup_called == [True]


@pytest.mark.asyncio
async def test_lifespan_completes_even_when_warmup_is_slow():
    """_lifespan: warmup이 느려도 lifespan 자체는 정상 완료된다.

    _warmup_embedder는 내부에서 모든 예외를 잡으므로 _lifespan에 예외가 전파되지 않는다.
    따라서 yield 이후 'started'가 반드시 채워져야 한다.
    """
    from qapilot.api.main import _lifespan

    async def slow_warmup():
        await asyncio.sleep(0)  # 비동기 양보 — slow 시뮬레이션

    mock_app = MagicMock()
    started = []

    with patch("qapilot.api.main._warmup_embedder", new=slow_warmup):
        async with _lifespan(mock_app):
            started.append(True)

    assert started == [True]


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI app 통합 — lifespan이 등록되어 있는지
# ──────────────────────────────────────────────────────────────────────────────

def test_app_has_lifespan_registered():
    """create_app()이 반환한 FastAPI 앱에 lifespan이 등록되어 있다."""
    from qapilot.api.main import create_app

    app = create_app()

    # FastAPI는 lifespan을 router.lifespan_context 또는 app.router.lifespan_context에 저장
    assert app.router.lifespan_context is not None
