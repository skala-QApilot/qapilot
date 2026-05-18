"""공통 에러 정의.

에러 코드 체계: AGENT_XXX / TOOL_XXX / SYSTEM_XXX / INTERNAL_AUTH_XXX

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


class AuthError(QApilotError):
    """인증/인가 에러."""

    pass


# 에러 코드 상수
class ErrorCode:
    # Agent
    AGENT_001 = "AGENT_001"  # Agent 일반 실행 실패
    AGENT_002 = "AGENT_002"  # LLM 호출 실패
    AGENT_003 = "AGENT_003"  # 타임아웃
    AGENT_004 = "AGENT_004"  # 출력 스키마 검증 실패
    AGENT_005 = "AGENT_005"  # 재시도 소진

    # Agent API
    AGENT_API_001 = "AGENT_API_001"  # 파이프라인 실행 실패
    AGENT_API_002 = "AGENT_API_002"  # trace 없음
    AGENT_API_003 = "AGENT_API_003"  # 잘못된 요청

    # Tool
    TOOL_001 = "TOOL_001"  # Tool 일반 실행 실패
    TOOL_002 = "TOOL_002"  # 파일 접근 실패
    TOOL_003 = "TOOL_003"  # DB 접근 실패
    TOOL_004 = "TOOL_004"  # 네트워크 실패
    TOOL_005 = "TOOL_005"  # 파싱 실패

    # UI Test Tool 전용 (FR-006) — UIStepResult.error 카테고리 prefix.
    # CrossCheckAgent (PR #62 의 UI 에러 코드 추출) 가 step 에러를 분류할 때 사용한다.
    TOOL_UI_LOCATOR_NOT_FOUND = "TOOL_UI_LOCATOR_NOT_FOUND"  # selector 가 화면에 없음 (locator 타임아웃)
    TOOL_UI_TIMEOUT = "TOOL_UI_TIMEOUT"  # 페이지 로드 / wait 타임아웃
    TOOL_UI_ASSERTION_FAIL = "TOOL_UI_ASSERTION_FAIL"  # expect(...) 실패
    TOOL_UI_NAVIGATION_FAIL = "TOOL_UI_NAVIGATION_FAIL"  # navigate URL 접속 실패
    TOOL_UI_UNSUPPORTED_ACTION = "TOOL_UI_UNSUPPORTED_ACTION"  # 정규화 통과 후 미지원
    TOOL_UI_FALLBACK_USED = "TOOL_UI_FALLBACK_USED"  # 1-step fallback 적용 (모호 케이스)
    TOOL_UI_UNKNOWN = "TOOL_UI_UNKNOWN"  # 분류 외 일반 예외

    # System
    SYSTEM_001 = "SYSTEM_001"  # 설정 오류
    SYSTEM_002 = "SYSTEM_002"  # 예산 초과
    SYSTEM_003 = "SYSTEM_003"  # 파이프라인 중단

    # Internal auth
    INTERNAL_AUTH_001 = "INTERNAL_AUTH_001"  # 내부 API 토큰 없음/불일치
