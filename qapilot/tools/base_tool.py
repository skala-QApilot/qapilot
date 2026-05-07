"""BaseTool 추상 클래스.

모든 Tool은 이 클래스를 상속하여 run()을 구현한다.
Tool은 결정적(deterministic)이며 LLM을 호출하지 않는다.

Author: 공통
Created: 2026-05-07
"""

from abc import ABC, abstractmethod

from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import ToolInput, ToolOutput


class BaseTool(ABC):
    """Tool 공통 추상 클래스.

    모든 Tool은 이 클래스를 상속하며 다음 규칙을 준수한다:
    - 동일 입력 → 동일 출력 (결정적 실행)
    - LLM 호출 금지
    - 다른 Tool/Agent를 호출하지 않음
    """

    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id
        self.logger = get_logger(source=self.__class__.__name__, trace_id=trace_id)

    @abstractmethod
    async def run(self, input: ToolInput) -> ToolOutput:
        """Tool 실행. 하위 클래스에서 구현한다."""
        raise NotImplementedError
