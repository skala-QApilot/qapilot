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
from qapilot.shared.errors import AgentExecutionError, ErrorCode
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
        재시도 시 직전 에러 메시지를 last_error로 _execute()에 전달하여 자가 수정을 유도한다.
        """
        start = time.monotonic()
        self.logger.info("agent_start", params_keys=list(input.params.keys()))

        # 입력 가드레일
        try:
            Guardrails.check_input({**input.context, **input.params})
        except AgentExecutionError as e:
            self.logger.error("input_guardrail_blocked", code=e.code, detail=e.context)
            raise

        # 재시도 루프
        last_error: str | None = None
        retry_count = 0

        for attempt in range(self._resolved.max_retry + 1):
            retry_count = attempt
            try:
                execute_result = await self._execute(input.context, input.params, last_error=last_error)

                if not isinstance(execute_result, ExecuteResult):
                    raise AgentExecutionError(
                        ErrorCode.AGENT_004, "_execute()는 ExecuteResult를 반환해야 합니다",
                    )

                # 출력 가드레일
                try:
                    Guardrails.check_output(execute_result.result)
                except AgentExecutionError as e:
                    self.logger.error("output_guardrail_blocked", code=e.code, detail=e.context)
                    raise

                # 메타데이터 수집
                duration = round(time.monotonic() - start, 2)
                metadata = BaseMetadata(
                    model=self._resolved.model,
                    tokens_used=self.llm.total_tokens,
                    cost_usd=self.llm.total_cost_usd,
                    duration_sec=duration,
                    retry_count=retry_count,
                    cache_hit=self.llm.cache_hit,
                )

                self.logger.info(
                    "agent_complete",
                    confidence=execute_result.confidence,
                    duration_sec=duration,
                    tokens=metadata.tokens_used,
                    cost_usd=metadata.cost_usd,
                    retries=retry_count,
                )

                return AgentOutput(
                    trace_id=input.trace_id,
                    result=execute_result.result,
                    confidence=execute_result.confidence,
                    metadata=metadata,
                )

            except AgentExecutionError:
                raise  # 가드레일/검증 에러는 재시도하지 않음

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                self.logger.warning("agent_retry", attempt=attempt, error=str(e))

            # 다음 재시도 전 대기
            if attempt < self._resolved.max_retry:
                await asyncio.sleep(2 ** (attempt + 1))

        self.logger.error(
            "agent_failed",
            retries=self._resolved.max_retry + 1,
            last_error=last_error,
        )
        raise AgentExecutionError(
            ErrorCode.AGENT_005,
            f"재시도 {self._resolved.max_retry + 1}회 모두 실패",
            {"last_error": last_error},
        )

    @abstractmethod
    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """Agent 비즈니스 로직. 하위 클래스에서 구현한다.

        self.llm, self.prompts를 사용하여 LLM 호출 및 프롬프트 렌더링 가능.

        Args:
            context: 컨텍스트 데이터 (코드베이스, 도메인 등).
            params: 입력 파라미터.
            last_error: 이전 시도에서 발생한 에러 메시지. None이면 첫 시도.
                self.with_correction_hint()으로 프롬프트에 첨부하면 LLM 자가 수정 유도 가능.

        Returns:
            ExecuteResult(result=dict, confidence=float).
        """

    def with_correction_hint(self, prompt: str, last_error: str | None) -> str:
        """프롬프트에 자가 수정 힌트를 추가한다.

        팀원이 _execute() 내부에서 사용한다:
            user_prompt = self.with_correction_hint(self.prompts.render(...), last_error)

        Args:
            prompt: 원본 프롬프트.
            last_error: 이전 시도의 에러 메시지. None이면 원본 그대로 반환.

        Returns:
            last_error가 있으면 힌트가 추가된 프롬프트.
        """
        if not last_error:
            return prompt
        return (
            f"{prompt}\n\n"
            f"⚠️ 이전 시도에서 다음 오류가 발생했습니다:\n{last_error}\n\n"
            f"출력 스키마와 형식을 정확히 준수하여 다시 작성하세요."
        )

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
            self.logger.error("tool_denied", tool=tool_name, allowed=self.allowed_tools)
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
