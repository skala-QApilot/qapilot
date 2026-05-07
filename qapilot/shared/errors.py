"""공통 에러 정의.

에러 코드 체계: AGENT_XXX / TOOL_XXX / SYSTEM_XXX

Author: 공통
Created: 2026-05-07
"""


class QApilotError(Exception):
    """QApilot 기본 에러."""

    def __init__(self, code: str, message: str, context: dict | None = None):
        self.code = code
        self.message = message
        self.context = context or {}
        super().__init__(f"[{code}] {message}")


class AgentExecutionError(QApilotError):
    """Agent 실행 에러."""

    pass


class AgentTimeoutError(QApilotError):
    """Agent 타임아웃 에러."""

    pass


class ToolExecutionError(QApilotError):
    """Tool 실행 에러."""

    pass


class LLMApiError(QApilotError):
    """LLM API 호출 에러."""

    pass


class HITLRejectError(QApilotError):
    """HITL 반려 에러."""

    pass


# 에러 코드 상수
class ErrorCode:
    # Agent
    AGENT_001 = "AGENT_001"  # Agent 일반 실행 실패
    AGENT_002 = "AGENT_002"  # LLM 호출 실패
    AGENT_003 = "AGENT_003"  # 타임아웃
    AGENT_004 = "AGENT_004"  # 출력 스키마 검증 실패
    AGENT_005 = "AGENT_005"  # 재시도 소진

    # Tool
    TOOL_001 = "TOOL_001"  # Tool 일반 실행 실패
    TOOL_002 = "TOOL_002"  # 파일 접근 실패
    TOOL_003 = "TOOL_003"  # DB 접근 실패
    TOOL_004 = "TOOL_004"  # 네트워크 실패
    TOOL_005 = "TOOL_005"  # 파싱 실패

    # System
    SYSTEM_001 = "SYSTEM_001"  # 설정 오류
    SYSTEM_002 = "SYSTEM_002"  # 예산 초과
    SYSTEM_003 = "SYSTEM_003"  # 파이프라인 중단
