"""HITL 모듈.

시나리오 생성(FR-002)과 자연어 해석(FR-003) 결과의
승인/수정/반려 기능을 제공한다. 이 2곳에서만 HITL이 발생한다.

담당: A
Created: 2026-05-07
"""


class HITLModule:
    """HITL 검토 모듈.

    적용 대상: scenario_gen, natural_lang (2곳만)
    나머지 Agent는 자체 Fallback 처리.
    야간 실행(스케줄링)에서는 HITL 미발생.
    """

    async def submit_for_review(self, target: str, target_id: str, data: dict) -> None:
        """검토 큐에 적재한다."""
        raise NotImplementedError

    async def get_pending(self) -> list[dict]:
        """대기 중인 검토 목록을 반환한다."""
        raise NotImplementedError

    async def approve(self, review_id: str) -> None:
        """승인 처리한다."""
        raise NotImplementedError

    async def reject(self, review_id: str, reason: str) -> None:
        """반려 처리한다."""
        raise NotImplementedError
