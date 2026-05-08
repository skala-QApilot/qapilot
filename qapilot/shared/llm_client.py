"""OpenAI API 클라이언트 래퍼.

langchain-openai ChatOpenAI 기반. 재시도(exponential backoff),
토큰 카운팅, 실행 내 캐싱을 제공한다.

Author: A
Created: 2026-05-07
"""

import asyncio
import hashlib
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from qapilot.shared.config import LLMConfig
from qapilot.shared.errors import ErrorCode, LLMApiError, QApilotError
from qapilot.shared.logger import get_logger

_MAX_LLM_RETRY = 3
_BACKOFF_BASE_SEC = 1


@dataclass
class LLMResponse:
    """LLM 응답."""

    content: str
    model: str
    tokens_used: int
    cached: bool


class LLMClient:
    """LLM 호출 클라이언트. BaseAgent에서 self.llm으로 사용된다."""

    def __init__(self, config: LLMConfig, trace_id: str | None = None):
        self._config = config
        self._logger = get_logger(source="llm_client", trace_id=trace_id)
        self._cache: dict[str, LLMResponse] = {}
        self.total_tokens: int = 0
        self.cache_hit: bool = False

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """LLM 채팅 호출. 재시도와 캐싱을 자동 처리한다.

        Args:
            system_prompt: 시스템 메시지.
            user_prompt: 사용자 메시지.
            model: 모델 오버라이드. 기본값은 config.default_model.
            temperature: 샘플링 온도. 기본 0.0 (결정적).

        Returns:
            LLMResponse.

        Raises:
            LLMApiError: 재시도 소진 시.
        """
        # 토큰 예산 차단
        if self.total_tokens >= self._config.max_tokens_per_task:
            raise QApilotError(
                ErrorCode.SYSTEM_002,
                f"태스크 토큰 예산 초과: {self.total_tokens}/{self._config.max_tokens_per_task}",
                {"used": self.total_tokens, "limit": self._config.max_tokens_per_task},
            )

        model = model or self._config.default_model
        cache_key = self._cache_key(system_prompt, user_prompt, model)

        if cache_key in self._cache:
            self.cache_hit = True
            self._logger.debug("llm_cache_hit", model=model)
            return self._cache[cache_key]

        response = await self._call_with_retry(system_prompt, user_prompt, model, temperature)
        self.total_tokens += response.tokens_used
        self._cache[cache_key] = response
        return response

    async def _call_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> LLMResponse:
        """재시도 로직 (3회, 1s/2s/4s backoff)."""
        llm = ChatOpenAI(model=model, temperature=temperature)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]

        last_error: Exception | None = None
        for attempt in range(_MAX_LLM_RETRY):
            try:
                result = await llm.ainvoke(messages)
                tokens = result.usage_metadata.get("total_tokens", 0) if result.usage_metadata else 0
                return LLMResponse(
                    content=result.content,
                    model=model,
                    tokens_used=tokens,
                    cached=False,
                )
            except Exception as e:
                last_error = e
                self._logger.warning(
                    "llm_retry", attempt=attempt, error=str(e), model=model,
                )
                if attempt < _MAX_LLM_RETRY - 1:
                    await asyncio.sleep(_BACKOFF_BASE_SEC * (2**attempt))

        raise LLMApiError(
            ErrorCode.AGENT_002,
            f"LLM 호출 {_MAX_LLM_RETRY}회 실패",
            {"model": model, "last_error": str(last_error)},
        )

    @staticmethod
    def _cache_key(system: str, user: str, model: str) -> str:
        """프롬프트 해시로 캐시 키를 생성한다."""
        raw = f"{model}:{system}:{user}"
        return hashlib.sha256(raw.encode()).hexdigest()
