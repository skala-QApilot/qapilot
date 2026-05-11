"""스케줄링 모듈.

검토 완료된 시나리오 그룹을 자동으로 배치 실행한다 (완전 무인).

담당: (공통)
Created: 2026-05-07
"""


class ScheduleModule:
    """스케줄링 모듈.

    완전 무인 실행 (승인된 시나리오만 실행).
    실패 케이스 자동 재시도 (최대 3회).
    완료 시 리포트 자동 생성.
    """

    async def run_scheduled(self, scenario_group: list[str] | None = None) -> None:
        """스케줄에 따라 테스트를 실행한다."""
        raise NotImplementedError
