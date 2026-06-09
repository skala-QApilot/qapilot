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

# 카드번호 패턴 — scenario_generator/code_generator는 결제 테스트 시나리오 등에서
# "의도적으로" 더미 카드번호 형식의 합성 테스트 데이터를 만들어내므로 별도 취급한다
# (정상 기능 출력을 민감정보 누출로 오탐해 파이프라인 전체를 실패시키는 문제 — 이슈 #181).
_CARD_NUMBER_PATTERN = re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b")

# 카드번호 패턴 검사를 건너뛸 에이전트 (BaseAgent._agent_name 기준).
# 합성 테스트 데이터 생성이 본연의 기능이라 false positive 비용이 더 크다.
_CARD_CHECK_EXEMPT_AGENTS: frozenset[str] = frozenset({"scenario_generator", "code_generator"})

# 민감정보 패턴
_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),  # OpenAI API key
    re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),  # GitHub token
    re.compile(r"postgresql://\S+:\S+@"),  # DB 접속 URL
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\b\d{6}[-\s]?\d{7}\b"),  # 주민등록번호 패턴
    _CARD_NUMBER_PATTERN,
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
    def check_output(data: dict[str, Any], agent_name: str | None = None) -> None:
        """출력 데이터에서 민감정보 누출을 감지한다.

        agent_name이 _CARD_CHECK_EXEMPT_AGENTS에 속하면 카드번호 패턴 검사는
        건너뛴다 (해당 에이전트는 합성 테스트 데이터로 카드번호 형식 값을
        의도적으로 생성하므로, 그대로 검사하면 정상 출력을 누출로 오탐한다).

        Raises:
            AgentExecutionError: 민감정보 탐지 시.
        """
        skip_card_check = agent_name in _CARD_CHECK_EXEMPT_AGENTS
        for text in _extract_strings(data):
            for pattern in _SENSITIVE_PATTERNS:
                if pattern is _CARD_NUMBER_PATTERN and skip_card_check:
                    continue
                if pattern.search(text):
                    raise AgentExecutionError(
                        ErrorCode.AGENT_004,
                        f"출력 가드레일 위반: 민감정보 누출 탐지",
                        {"pattern": pattern.pattern},
                    )
