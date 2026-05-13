"""API 추적 Tool.

Playwright 네트워크 리스너로 요청/응답을 캡처하고
trace_id 기준으로 UI 스텝과 API 호출을 매칭한다.

담당: E
Created: 2026-05-07
"""

import json
from datetime import datetime, timezone

from playwright.async_api import Page, Request, Response

from qapilot.shared.schemas import APICall, APITraceResult, ToolInput, ToolOutput
from qapilot.tools.base_tool import BaseTool

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB


class APITraceTool(BaseTool):
    """API 추적 Tool.

    역할: 네트워크 리스너, 요청/응답 수집, trace_id 매칭
    입력: Playwright page 인스턴스 (UI 테스트와 동일 브라우저)
    출력: List[APITraceResult]
    제한: 10MB 초과 응답 body는 저장하지 않음
    """

    def __init__(self, trace_id: str | None = None):
        super().__init__(trace_id=trace_id)
        self._calls: list[APICall] = []
        self._request_times: dict[str, datetime] = {}

    def _on_request(self, request: Request) -> None:
        """요청 시작 시간 기록."""
        self._request_times[request.url] = datetime.now(timezone.utc)

    async def _on_response(self, response: Response) -> None:
        """응답 캡처."""
        url = response.url

        # 시작 시간으로 latency 계산
        start = self._request_times.pop(url, datetime.now(timezone.utc))
        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

        # response body (10MB 초과 시 저장 안 함)
        response_body = None
        try:
            body_bytes = await response.body()
            if len(body_bytes) <= MAX_BODY_SIZE:
                response_body = json.loads(body_bytes.decode("utf-8", errors="ignore"))
        except Exception:
            pass

        # request body
        request_body = None
        try:
            post_data = response.request.post_data
            if post_data:
                request_body = json.loads(post_data)
        except Exception:
            pass

        call = APICall(
            timestamp=datetime.now(timezone.utc).isoformat(),
            method=response.request.method,
            url=url,
            request_headers=dict(response.request.headers),
            request_body=request_body,
            status_code=response.status,
            response_body=response_body,
            response_size=len(await response.body()) if response_body else 0,
            content_type=response.headers.get("content-type", ""),
            latency_ms=latency_ms,
            matched_step_no=None,
        )
        self._calls.append(call)

    async def run(self, input: ToolInput) -> ToolOutput:
        """Playwright page에 네트워크 리스너 등록하고 API 호출 캡처."""
        page: Page = input.params.get("page")
        tc_id: str = input.params.get("tc_id", "unknown")

        if page is None:
            raise ValueError("params에 'page' (Playwright Page 인스턴스) 필요")

        # 리스너 초기화
        self._calls = []
        self._request_times = {}

        # 네트워크 리스너 등록
        page.on("request", self._on_request)
        page.on("response", self._on_response)

        # 테스트 실행은 외부(UI Test Tool)에서 함
        # 여기선 리스너만 등록하고 결과 반환
        error_calls = sum(1 for c in self._calls if c["status_code"] >= 400)

        result = APITraceResult(
            tc_id=tc_id,
            calls=self._calls,
            total_calls=len(self._calls),
            error_calls=error_calls,
        )

        return ToolOutput(
            trace_id=input.trace_id,
            result={"api_trace": result},
            metadata={"total_calls": len(self._calls)},
        )