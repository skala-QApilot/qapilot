"""BaseAgent 실행 하네스.

팀원은 _execute()만 구현한다.
run()이 입력 검증, 가드레일, 재시도, 타임아웃, 출력 검증,
메타데이터 수집, 구조화 로깅을 자동 처리한다.

Author: A
Created: 2026-05-07
"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from qapilot.shared.config import (
    QApilotConfig,
    class_name_to_snake,
    load_config,
    resolve_agent_config,
)
from qapilot.shared.errors import AgentExecutionError, AgentTimeoutError, ErrorCode
from qapilot.shared.guardrails import Guardrails
from qapilot.shared.llm_client import LLMClient
from qapilot.shared.logger import get_logger
from qapilot.shared.prompt_loader import PromptLoader
from qapilot.shared.schemas import AgentInput, AgentOutput, BaseMetadata, ExecuteResult


class BaseAgent(ABC):
    """Agent 실행 하네스.

    하위 클래스는 _execute()만 구현한다.
    run()이 가드레일, 재시도, 타임아웃, 로깅을 자동 처리한다.

    클래스 속성 (하위 클래스에서 오버라이드):
        agent_name: prompts/ 디렉토리명. 미설정 시 클래스명에서 자동 파생.
        allowed_tools: 호출 가능 Tool 화이트리스트.
    """

    agent_name: str = ""
    allowed_tools: list[str] = []

    def __init__(self, trace_id: str | None = None, config: QApilotConfig | None = None):
        self.trace_id = trace_id
        self._config = config or load_config()
        self._agent_name = self.agent_name or class_name_to_snake(self.__class__.__name__)
        self._resolved = resolve_agent_config(self._agent_name, self._config)
        self.logger = get_logger(source=self._agent_name, trace_id=trace_id)
        self.llm = LLMClient(self._config.llm, trace_id=trace_id)
        self.prompts = PromptLoader(self._agent_name)

    async def run(self, input: AgentInput) -> AgentOutput:
        """Agent 실행 하네스. 팀원이 건드리지 않는 영역.

        흐름: 입력 가드레일 → 재시도 루프(타임아웃 포함) → 출력 가드레일 → 메타데이터 → 로깅
        """
        start = time.monotonic()
        self.logger.info("agent_start", params_keys=list(input.params.keys()))

        # 입력 가드레일
        Guardrails.check_input({**input.context, **input.params})

        # 재시도 루프
        last_error: Exception | None = None
        retry_count = 0

        for attempt in range(self._resolved.max_retry + 1):
            retry_count = attempt
            try:
                execute_result = await asyncio.wait_for(
                    self._execute(input.context, input.params),
                    timeout=self._resolved.timeout_sec,
                )

                if not isinstance(execute_result, ExecuteResult):
                    raise AgentExecutionError(
                        ErrorCode.AGENT_004, "_execute()는 ExecuteResult를 반환해야 합니다",
                    )

                # 출력 가드레일
                Guardrails.check_output(execute_result.result)

                # 메타데이터 수집
                duration = round(time.monotonic() - start, 2)
                metadata = BaseMetadata(
                    model=self._resolved.model,
                    tokens_used=self.llm.total_tokens,
                    duration_sec=duration,
                    retry_count=retry_count,
                    cache_hit=self.llm.cache_hit,
                )

                self.logger.info(
                    "agent_complete",
                    confidence=execute_result.confidence,
                    duration_sec=duration,
                    tokens=metadata.tokens_used,
                    retries=retry_count,
                )

                return AgentOutput(
                    trace_id=input.trace_id,
                    result=execute_result.result,
                    confidence=execute_result.confidence,
                    metadata=metadata,
                )

            except asyncio.TimeoutError:
                last_error = AgentTimeoutError(
                    ErrorCode.AGENT_003,
                    f"타임아웃: {self._resolved.timeout_sec}초 초과",
                    {"attempt": attempt},
                )
                self.logger.warning("agent_timeout", attempt=attempt)

            except AgentExecutionError:
                raise  # 가드레일/검증 에러는 재시도하지 않음

            except Exception as e:
                last_error = e
                self.logger.warning("agent_retry", attempt=attempt, error=str(e))

            # 다음 재시도 전 대기
            if attempt < self._resolved.max_retry:
                await asyncio.sleep(2 ** (attempt + 1))

        raise AgentExecutionError(
            ErrorCode.AGENT_005,
            f"재시도 {self._resolved.max_retry + 1}회 모두 실패",
            {"last_error": str(last_error)},
        )

    @abstractmethod
    async def _execute(
        self, context: dict[str, Any], params: dict[str, Any]
    ) -> ExecuteResult:
        """Agent 비즈니스 로직. 하위 클래스에서 구현한다.

        self.llm, self.prompts를 사용하여 LLM 호출 및 프롬프트 렌더링 가능.

        Args:
            context: 컨텍스트 데이터 (코드베이스, 도메인 등).
            params: 입력 파라미터.

        Returns:
            ExecuteResult(result=dict, confidence=float).
        """

    async def use_tool(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Tool을 호출한다. allowed_tools 화이트리스트를 검증한다.

        Args:
            tool_name: Tool 등록명 (예: "codebase_scanner").
            params: Tool 입력 파라미터.

        Returns:
            Tool 실행 결과 dict.

        Raises:
            AgentExecutionError: 화이트리스트에 없는 Tool 호출 시.
        """
        if tool_name not in self.allowed_tools:
            raise AgentExecutionError(
                ErrorCode.AGENT_001,
                f"Tool '{tool_name}'은 허용 목록에 없습니다: {self.allowed_tools}",
            )
        from qapilot.tools import TOOL_REGISTRY
        from qapilot.shared.schemas import ToolInput

        tool_cls = TOOL_REGISTRY.get(tool_name)
        if tool_cls is None:
            raise AgentExecutionError(
                ErrorCode.AGENT_001, f"Tool '{tool_name}'이 등록되지 않았습니다",
            )
        tool = tool_cls(trace_id=self.trace_id, config=self._config)
        tool_input = ToolInput(trace_id=self.trace_id or "", params=params)
        output = await tool.run(tool_input)
        return output.result
