"""BaseTool 실행 하네스.

팀원은 _execute()만 구현한다.
run()이 타임아웃, 에러 래핑, 메타데이터 수집, 로깅을 자동 처리한다.

Author: A
Created: 2026-05-07
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from qapilot.shared.config import QApilotConfig, load_config
from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.shared.logger import get_logger
from qapilot.shared.schemas import ToolInput, ToolOutput


class BaseTool(ABC):
    """Tool 실행 하네스.

    하위 클래스는 _execute()만 구현한다.
    run()이 타임아웃, 에러 래핑, 로깅을 자동 처리한다.
    Tool은 결정적이므로 재시도/가드레일 없음.
    """

    def __init__(self, trace_id: str | None = None, config: QApilotConfig | None = None):
        self.trace_id = trace_id
        self._config = config or load_config()
        self.logger = get_logger(source=self.__class__.__name__, trace_id=trace_id)

    async def run(self, input: ToolInput) -> ToolOutput:
        """Tool 실행 하네스.

        Args:
            input: ToolInput (trace_id, params).

        Returns:
            ToolOutput (trace_id, result, metadata).
        """
        start = time.monotonic()
        self.logger.info("tool_start", params_keys=list(input.params.keys()))

        try:
            result = await asyncio.wait_for(
                self._execute(input.params),
                timeout=self._config.agent.timeout_sec,
            )
        except asyncio.TimeoutError:
            self.logger.error("tool_timeout")
            raise ToolExecutionError(
                ErrorCode.TOOL_001,
                f"Tool 타임아웃: {self._config.agent.timeout_sec}초 초과",
            )
        except ToolExecutionError:
            raise
        except Exception as e:
            self.logger.error("tool_error", error=str(e))
            raise ToolExecutionError(
                ErrorCode.TOOL_001, f"Tool 실행 실패: {e}",
            ) from e

        duration = round(time.monotonic() - start, 2)
        self.logger.info("tool_complete", duration_sec=duration)

        return ToolOutput(
            trace_id=input.trace_id,
            result=result,
            metadata={"duration_sec": duration},
        )

    @abstractmethod
    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Tool 비즈니스 로직. 하위 클래스에서 구현한다.

        Args:
            params: 입력 파라미터.

        Returns:
            결과 dict.
        """
