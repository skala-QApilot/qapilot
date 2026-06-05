"""FastAPI 앱 생성.

담당: C
Created: 2026-05-07
"""

import structlog
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request

from qapilot.api.agent_router import router as agent_router
from qapilot.api.response import fail
from qapilot.modules.trace_module import TraceModule
from qapilot.shared.errors import AuthError, QApilotError
from qapilot.shared.logger import setup_logger

_TRACE_HEADER = "X-Trace-Id"


async def _trace_id_middleware(request: Request, call_next):
    """요청마다 trace_id 를 확정하고 (헤더 우선, 없으면 발급) 응답 헤더로 돌려준다.

    확정된 trace_id 는 contextvars 에 바인딩되어 해당 요청 처리 중 발생하는
    모든 로그에 자동 포함되고, ``request.state.trace_id`` 로도 접근 가능하다.
    """
    trace_id = request.headers.get(_TRACE_HEADER) or TraceModule.generate_trace_id()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=trace_id)
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers[_TRACE_HEADER] = trace_id
    return response


def create_app() -> FastAPI:
    """FastAPI 앱을 생성한다."""
    # .env 파일을 시작 시 1회 로드 — verify_internal_token 등 요청 처리 시점에
    # os.getenv 로 환경변수를 읽는 경로가 있어 반드시 부팅 시 채워줘야 한다.
    load_dotenv(find_dotenv(usecwd=False))
    setup_logger()

    # DB / S3 pool lazy init 을 startup 에 강제 트리거 — 환경변수 누락이나
    # 연결 실패를 즉시 로그로 가시화한다. 실패해도 graceful no-op 그대로 (file 기록은 동작).
    from qapilot.db.connection import get_pool
    from qapilot.storage.s3_client import get_client
    get_pool()
    get_client()
    app = FastAPI(title="QApilot", version="0.1.0")

    app.middleware("http")(_trace_id_middleware)
    app.exception_handler(AuthError)(_auth_error_handler)
    app.exception_handler(QApilotError)(_qapilot_error_handler)

    @app.get("/health")
    async def health():
        """서버 상태를 확인한다."""
        return {"status": "ok", "version": "0.1.0"}

    # 최종 FastAPI는 AI 실행 서버로 축소한다.
    app.include_router(agent_router)

    # React 빌드 결과물 정적 서빙
    # app.mount("/", StaticFiles(directory="qapilot/web/dist", html=True))

    return app


async def _auth_error_handler(request: Request, exc: AuthError):
    """AuthError를 공통 실패 응답으로 변환한다."""
    return fail(exc.code, exc.message)


async def _qapilot_error_handler(request: Request, exc: QApilotError):
    """QApilotError를 공통 실패 응답으로 변환한다."""
    return fail(exc.code, exc.message)


app = create_app()
