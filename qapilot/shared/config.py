"""설정 로드 모듈.

qapilot.config.yaml + .env 파일에서 설정을 로드한다.

Author: 공통
Created: 2026-05-07
"""

import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    default_model: str = "gpt-4o-mini"
    deep_model: str = "gpt-4o"
    monthly_budget_usd: float = 500
    max_tokens_per_task: int = 200000


class AgentOverride(BaseModel):
    """Agent별 설정 오버라이드."""

    timeout_sec: int | None = None
    max_retry: int | None = None
    model: str | None = None
    allowed_tools: list[str] | None = None


class AgentConfig(BaseModel):
    timeout_sec: int = 120
    max_retry: int = 3
    overrides: dict[str, AgentOverride] = Field(default_factory=dict)


class ResolvedAgentConfig(BaseModel):
    """글로벌 기본값 + Agent별 오버라이드가 병합된 최종 설정."""

    timeout_sec: int
    max_retry: int
    model: str
    allowed_tools: list[str]


class TestConfig(BaseModel):
    page_load_timeout_sec: int = 30
    step_timeout_sec: int = 30
    headless: bool = True


class DashboardConfig(BaseModel):
    port: int = 7860


class ServerConfig(BaseModel):
    url: str | None = None
    token: str | None = None


class AuthConfig(BaseModel):
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7


class TestAccountConfig(BaseModel):
    """SUT 테스트 사용자 인증 정보 — UITestTool 의 precondition fail-safe 가 활용.

    이슈 #174 (격차 12 D 영역 본질): 인증 필요 시나리오의 TC 시작 시 페이지가
    `/login` 으로 redirect 되면 본 계정으로 자동 로그인 시도. 시나리오 책임
    경계 정합 — 시나리오 본문엔 로그인 step 없음 (Cucumber Background /
    pytest fixture / Playwright test.beforeEach 패턴).

    **Phase 1 (현재)**: qapilot.config.yaml 의 `project.test_account` 로 수동 입력.
    **Phase 2 (후속)**: GitCodebaseScannerTool 확장 — seed.py / fixtures / .env 자동 추출.
    """

    email: str | None = None
    password: str | None = None
    # 로그인 페이지 URL 패턴 (e.g. "/login", "/sign-in"). None 이면 기본 패턴 사용.
    login_path: str | None = None


class ProjectConfig(BaseModel):
    name: str | None = None
    target_url: str | None = None
    root: str | None = None
    repo_path: str | None = None
    test_account: TestAccountConfig = TestAccountConfig()


class QApilotConfig(BaseModel):
    project: ProjectConfig = ProjectConfig()
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
    test: TestConfig = TestConfig()
    dashboard: DashboardConfig = DashboardConfig()
    server: ServerConfig = ServerConfig()
    auth: AuthConfig = AuthConfig()


def load_config(config_path: Path | None = None) -> QApilotConfig:
    """qapilot.config.yaml에서 설정을 로드한다."""
    load_dotenv()

    if config_path is None:
        config_path = Path("qapilot.config.yaml")

    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return QApilotConfig(**raw)

    return QApilotConfig()


def resolve_agent_config(agent_name: str, config: QApilotConfig) -> ResolvedAgentConfig:
    """글로벌 기본값과 Agent별 오버라이드를 병합한다."""
    override = config.agent.overrides.get(agent_name, AgentOverride())
    return ResolvedAgentConfig(
        timeout_sec=override.timeout_sec or config.agent.timeout_sec,
        max_retry=override.max_retry or config.agent.max_retry,
        model=override.model or config.llm.default_model,
        allowed_tools=override.allowed_tools if override.allowed_tools is not None else [],
    )


def class_name_to_snake(name: str) -> str:
    """PascalCase 클래스명을 snake_case로 변환한다. Agent 접미사를 제거한다."""
    name = re.sub(r"Agent$", "", name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
