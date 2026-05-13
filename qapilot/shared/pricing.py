"""모델별 토큰 단가 및 비용 계산.

단가는 USD per 1,000 tokens 기준 (OpenAI 공개 가격, 2026-05 시점).
가격이 바뀌면 이 표만 수정하면 된다. 알 수 없는 모델은 0.0 으로 처리한다 —
토큰 수는 그대로 기록되므로 단가표를 채운 뒤 사후 보정이 가능하다.

Author: A
Created: 2026-05-12
"""

from __future__ import annotations

# 모델명 → (입력 단가, 출력 단가) — USD per 1K tokens
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    "o3-mini": (0.0011, 0.0044),
    "o4-mini": (0.0011, 0.0044),
    # 임베딩 모델 (출력 토큰 없음)
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
}

_UNKNOWN: tuple[float, float] = (0.0, 0.0)


def _rates_for(model: str) -> tuple[float, float]:
    """모델 단가를 조회한다. 정확히 없으면 접두사 일치(예: "gpt-4o-2024-08-06")를 시도한다."""
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for name, rates in MODEL_PRICING.items():
        if model.startswith(name):
            return rates
    return _UNKNOWN


def calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """입력/출력 토큰 수로 USD 비용을 계산한다 (소수점 6자리 반올림).

    Args:
        model: 모델명.
        input_tokens: 프롬프트 토큰 수.
        output_tokens: 생성 토큰 수.

    Returns:
        USD 비용. 알 수 없는 모델이면 0.0.
    """
    in_rate, out_rate = _rates_for(model)
    return round((input_tokens / 1000) * in_rate + (output_tokens / 1000) * out_rate, 6)
