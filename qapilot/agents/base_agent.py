"""BaseAgent 추상 클래스.

모든 Agent는 이 클래스를 상속하여 run()을 구현한다.

Author: 공통
Created: 2026-05-07
"""

from abc import ABC, abstractmethod

from qapilot.shared.errors import AgentExecutionError, AgentTimeoutError, LLMApiError
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import AgentInput, AgentOutput


class BaseAgent(ABC):
    """Agent 공통 추상 클래스.

    모든 Agent는 이 클래스를 상속하며 다음 규칙을 준수한다:
    - Agent 간 직접 호출 금지 (Orchestrator 경유)
    - 참조용 Tool만 직접 호출 가능
    - 출력에 confidence (0.0~1.0) 필수 포함
    - stateless 운영 (이전 실행을 기억하지 않음)
    """

    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id
        self.logger = get_logger(source=self.__class__.__name__, trace_id=trace_id)

    @abstractmethod
    async def run(self, input: AgentInput) -> AgentOutput:
        """Agent 실행. 하위 클래스에서 구현한다."""
        raise NotImplementedError
