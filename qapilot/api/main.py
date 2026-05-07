"""FastAPI 앱 생성.

담당: C
Created: 2026-05-07
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from qapilot.api.agent_router import router as agent_router
from qapilot.api.defect_router import router as defect_router
from qapilot.api.hitl_router import router as hitl_router
from qapilot.api.report_router import router as report_router
from qapilot.api.scenario_router import router as scenario_router


def create_app() -> FastAPI:
    """FastAPI 앱을 생성한다."""
    app = FastAPI(title="QApilot", version="0.1.0")

    # 라우터 등록
    app.include_router(agent_router)
    app.include_router(scenario_router)
    app.include_router(defect_router)
    app.include_router(hitl_router)
    app.include_router(report_router)

    # React 빌드 결과물 정적 서빙
    # app.mount("/", StaticFiles(directory="qapilot/web/dist", html=True))

    return app


app = create_app()
