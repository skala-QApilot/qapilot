"""입출력 가드레일.

프롬프트 인젝션 패턴과 민감정보 누출을 regex로 감지한다.
LLM 호출 없이 순수 패턴 매칭으로 동작한다.

Author: A
Created: 2026-05-07
"""

import re
from typing import Any

from qapilot.shared.errors import AgentExecutionError, ErrorCode

# 프롬프트 인젝션 패턴
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"^system\s*:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"act\s+as\s+(a\s+)?different", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.IGNORECASE),
]

# 민감정보 패턴
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),  # OpenAI API key
    re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),  # GitHub token
    re.compile(r"postgresql://\S+:\S+@"),  # DB 접속 URL
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\b\d{6}[-\s]?\d{7}\b"),  # 주민등록번호 패턴
    re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"),  # 카드번호
]


def _extract_strings(data: Any) -> list[str]:
    """중첩된 dict/list에서 모든 문자열 값을 추출한다."""
    strings: list[str] = []
    if isinstance(data, str):
        strings.append(data)
    elif isinstance(data, dict):
        for v in data.values():
            strings.extend(_extract_strings(v))
    elif isinstance(data, list):
        for item in data:
            strings.extend(_extract_strings(item))
    return strings


class Guardrails:
    """입출력 가드레일. Agent 하네스에서 자동 호출된다."""

    @staticmethod
    def check_input(data: dict[str, Any]) -> None:
        """입력 데이터에서 프롬프트 인젝션 패턴을 감지한다.

        Raises:
            AgentExecutionError: 인젝션 패턴 탐지 시.
        """
        for text in _extract_strings(data):
            for pattern in _INJECTION_PATTERNS:
                if pattern.search(text):
                    raise AgentExecutionError(
                        ErrorCode.AGENT_001,
                        f"입력 가드레일 위반: 프롬프트 인젝션 패턴 탐지",
                        {"pattern": pattern.pattern, "text": text[:100]},
                    )

    @staticmethod
    def check_output(data: dict[str, Any]) -> None:
        """출력 데이터에서 민감정보 누출을 감지한다.

        Raises:
            AgentExecutionError: 민감정보 탐지 시.
        """
        for text in _extract_strings(data):
            for pattern in _SENSITIVE_PATTERNS:
                if pattern.search(text):
                    raise AgentExecutionError(
                        ErrorCode.AGENT_004,
                        f"출력 가드레일 위반: 민감정보 누출 탐지",
                        {"pattern": pattern.pattern},
                    )
