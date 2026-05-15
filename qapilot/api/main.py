"""FastAPI 앱 생성.

담당: C
Created: 2026-05-07
"""

import structlog
from fastapi import FastAPI, Request

from qapilot.api.agent_router import router as agent_router
from qapilot.api.auth_router import router as auth_router
from qapilot.api.change_requests_router import router as change_requests_router
from qapilot.api.cli_router import router as cli_router
from qapilot.api.dashboard_extra_router import router as dashboard_extra_router
from qapilot.api.dashboard_router import router as dashboard_router
from qapilot.api.defect_router import router as defect_router
from qapilot.api.evidences_router import router as evidences_router
from qapilot.api.files_router import router as files_router
from qapilot.api.graph_router import router as graph_router
from qapilot.api.members_router import router as members_router
from qapilot.api.notifications_router import router as notifications_router
from qapilot.api.report_router import router as report_router
from qapilot.api.reports_router import router as reports_router
from qapilot.api.retest_router import router as retest_router
from qapilot.api.results_router import router as results_router
from qapilot.api.rtm_router import router as rtm_router
from qapilot.api.run_logs_router import router as run_logs_router
from qapilot.api.runs_router import router as runs_router
from qapilot.api.response import fail
from qapilot.api.scenario_groups_router import router as scenario_groups_router
from qapilot.api.scenario_router import router as scenario_router
from qapilot.api.scenario_versions_router import router as scenario_versions_router
from qapilot.api.service_router import router as service_router
from qapilot.api.service_scenario_router import router as service_scenario_router
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
    setup_logger()
    app = FastAPI(title="QApilot", version="0.1.0")

    app.middleware("http")(_trace_id_middleware)
    app.exception_handler(AuthError)(_auth_error_handler)
    app.exception_handler(QApilotError)(_qapilot_error_handler)

    @app.get("/health")
    async def health():
        """서버 상태를 확인한다."""
        return {"status": "ok", "version": "0.1.0"}

    # 라우터 등록
    app.include_router(auth_router)
    app.include_router(service_router)
    app.include_router(service_scenario_router)
    app.include_router(runs_router)
    app.include_router(dashboard_router)
    app.include_router(results_router)
    app.include_router(files_router)
    app.include_router(scenario_versions_router)
    app.include_router(change_requests_router)
    app.include_router(scenario_groups_router)
    app.include_router(rtm_router)
    app.include_router(evidences_router)
    app.include_router(reports_router)
    app.include_router(run_logs_router)
    app.include_router(notifications_router)
    app.include_router(members_router)
    app.include_router(retest_router)
    app.include_router(graph_router)
    app.include_router(dashboard_extra_router)
    app.include_router(cli_router)
    app.include_router(agent_router)
    app.include_router(scenario_router)
    app.include_router(defect_router)
    app.include_router(report_router)

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
