"""중앙 서버 API 클라이언트.

로컬의 시나리오, 테스트 결과 등을 중앙 서버로 전송한다.

담당: A
Created: 2026-05-07
"""

import os
from typing import Any, Dict

import httpx
from qapilot.shared.config import load_config
from qapilot.shared.logger import get_logger

logger = get_logger(__name__)


class ApiClient:
    """중앙 서버와 통신하는 클라이언트."""

    def __init__(self):
        config = load_config()
        # 환경변수 우선, 없으면 config.yaml 참조
        self.base_url = os.getenv("SERVER_URL") or config.server.url
        self.token = os.getenv("SERVER_TOKEN") or config.server.token
        
        if not self.base_url:
            logger.warning("SERVER_URL이 설정되지 않았습니다. 서버 동기화가 불가능합니다.")

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def upload_scenario(self, scenario_data: Dict[str, Any]) -> bool:
        """시나리오를 서버로 업로드한다."""
        if not self.base_url:
            return False

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/scenarios",
                    json=scenario_data,
                    headers=self._get_headers()
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"시나리오 업로드 실패: {e}")
                return False

    async def upload_results(self, trace_id: str, results: Dict[str, Any]) -> bool:
        """테스트 결과를 서버로 업로드한다."""
        if not self.base_url:
            return False

        async with httpx.AsyncClient() as client:
            try:
                # implementation-plan.md 사양에 따른 엔드포인트
                payload = {
                    "trace_id": trace_id,
                    "data": results
                }
                response = await client.post(
                    f"{self.base_url}/api/upload/results",
                    json=payload,
                    headers=self._get_headers()
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"테스트 결과 업로드 실패: {e}")
                return False

    async def upload_generated_code(self, tc_id: str, code: str) -> bool:
        """생성된 Playwright 코드를 서버로 업로드한다."""
        if not self.base_url:
            return False

        async with httpx.AsyncClient() as client:
            try:
                payload = {
                    "tc_id": tc_id,
                    "code": code
                }
                # 사양서 7.2절 기반 (코드 저장용 엔드포인트 제안)
                response = await client.post(
                    f"{self.base_url}/api/generated-code",
                    json=payload,
                    headers=self._get_headers()
                )
                response.raise_for_status()
                return True
            except Exception as e:
                logger.error(f"생성 코드 업로드 실패({tc_id}): {e}")
                return False

    async def check_health(self) -> bool:
        """서버 연결 상태를 확인한다."""
        if not self.base_url:
            return False

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"{self.base_url}/health")
                return response.status_code == 200
            except Exception:
                return False
