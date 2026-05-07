"""캐시 모듈.

.qapilot/ 디렉토리에 코드 분석 결과, 시나리오, LLM 응답을
캐싱하고 Git commit hash 기반 증분 재분석을 수행한다.

담당: (공통)
Created: 2026-05-07
"""


class CacheModule:
    """로컬 캐시 관리 모듈.

    저장 위치: .qapilot/cache/
    TTL 기반 만료 처리.
    Git commit hash 기반 증분 판단.
    """

    async def get(self, key: str) -> dict | None:
        """캐시에서 값을 조회한다."""
        raise NotImplementedError

    async def set(self, key: str, value: dict, ttl_hours: int = 24) -> None:
        """캐시에 값을 저장한다."""
        raise NotImplementedError

    async def invalidate(self, key: str) -> None:
        """캐시를 무효화한다."""
        raise NotImplementedError
