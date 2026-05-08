"""설정 로드 모듈.

qapilot.config.yaml + .env 파일에서 설정을 로드한다.

Author: 공통
Created: 2026-05-07
"""

from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class LLMConfig(BaseModel):
    default_model: str = "gpt-4o-mini"
    deep_model: str = "gpt-4o"
    monthly_budget_usd: float = 500
    max_tokens_per_task: int = 200000


class AgentConfig(BaseModel):
    timeout_sec: int = 60
    max_retry: int = 3


class TestConfig(BaseModel):
    page_load_timeout_sec: int = 30
    step_timeout_sec: int = 30
    headless: bool = True


class DashboardConfig(BaseModel):
    port: int = 7860


class ServerConfig(BaseModel):
    url: str | None = None
    token: str | None = None


class QApilotConfig(BaseModel):
    llm: LLMConfig = LLMConfig()
    agent: AgentConfig = AgentConfig()
    test: TestConfig = TestConfig()
    dashboard: DashboardConfig = DashboardConfig()
    server: ServerConfig = ServerConfig()


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
