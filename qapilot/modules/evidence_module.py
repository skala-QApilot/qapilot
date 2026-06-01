"""증적 캡처/보관 모듈.

모든 TC의 실행 결과를 성공/실패 무관하게
trace_id 기준으로 영구 보관한다.

담당: (공통)
Created: 2026-05-07
"""


class EvidenceModule:
    """증적 보관 모듈.

    저장 위치: .qapilot/evidence/{trace_id}/
    보관 항목: 스크린샷, API 요청/응답, SQL 로그, 콘솔 로그
    보존 기간: 24개월
    """

    async def save(self, trace_id: str, tc_id: str, evidence: dict) -> None:
        """증적을 저장한다."""
        raise NotImplementedError

    async def get(self, trace_id: str, tc_id: str) -> dict | None:
        """증적을 조회한다."""
        raise NotImplementedError
