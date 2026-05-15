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
    timeout_sec: int = 60
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


class ProjectConfig(BaseModel):
    name: str | None = None
    target_url: str | None = None
    root: str | None = None
    repo_path: str | None = None


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
