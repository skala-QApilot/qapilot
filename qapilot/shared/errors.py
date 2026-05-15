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

    # Auth
    AUTH_001 = "AUTH_001"  # 이메일/비밀번호 불일치
    AUTH_002 = "AUTH_002"  # 액세스 토큰 만료
    AUTH_003 = "AUTH_003"  # 토큰 무효/서명 불일치
    AUTH_004 = "AUTH_004"  # 권한 부족
    AUTH_005 = "AUTH_005"  # 사용자 없음

    # Service
    SERVICE_001 = "SERVICE_001"  # 서비스 없음
    SERVICE_002 = "SERVICE_002"  # 대상 경로 없음
    SERVICE_003 = "SERVICE_003"  # 서비스 생성 실패

    # Sync
    SYNC_001 = "SYNC_001"  # items 없음
    SYNC_002 = "SYNC_002"  # 저장 실패

    # Service scope scenarios / runs / dashboard
    SCENARIO_001 = "SCENARIO_001"  # 시나리오 없음
    SCENARIO_002 = "SCENARIO_002"  # 시나리오 저장 실패
    TC_001 = "TC_001"  # TC 없음
    RUN_001 = "RUN_001"  # 실행 없음
    DASHBOARD_001 = "DASHBOARD_001"  # 대시보드 집계 실패

    # Round 1 service APIs
    RESULT_001 = "RESULT_001"  # 결과 없음
    FILE_001 = "FILE_001"  # 파일 없음
    FILE_002 = "FILE_002"  # 파일 저장 실패
    VERSION_001 = "VERSION_001"  # 버전 없음
    CHANGE_REQUEST_001 = "CHANGE_REQUEST_001"  # 변경 요청 없음
    CHANGE_REQUEST_002 = "CHANGE_REQUEST_002"  # 잘못된 status 값
    GROUP_001 = "GROUP_001"  # 그룹 없음
    GROUP_002 = "GROUP_002"  # 스케줄 생성 실패

    # Round 2 service APIs
    RTM_001 = "RTM_001"  # RTM 버전 없음
    RTM_002 = "RTM_002"  # 요구사항 없음
    EVIDENCE_001 = "EVIDENCE_001"  # 증적 없음
    REPORT_001 = "REPORT_001"  # 리포트 없음

    # Round 3 service APIs
    NOTIFICATION_001 = "NOTIFICATION_001"  # 알림 없음
    MEMBER_001 = "MEMBER_001"  # 멤버 없음
    MEMBER_002 = "MEMBER_002"  # 멤버 초대 실패
    RETEST_001 = "RETEST_001"  # 재테스트 그룹 없음
    RETEST_002 = "RETEST_002"  # failed_tc_ids 없음

    # Test Variables
    TV_001 = "TV_001"  # TV 없음
    TV_002 = "TV_002"  # TV 저장 실패
