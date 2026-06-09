"""도메인 지식 Tool — 용어사전·규칙 파일 관리.

glossary.json(코드명↔업무명 매핑)과 rules.json(자연어 규칙)의
읽기·쓰기를 담당한다.

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations


class GlossaryManager:
    """용어사전·규칙 관리 (인메모리 전용).

    파일 저장 없이 쿼리 치환만 제공한다.
    """

    def load_glossary(self) -> dict[str, str]:
        return {}

    def save_glossary(self, _: dict[str, str]) -> None:
        pass

    def expand(self, text: str) -> str:
        return text

    def load_rules(self) -> list[dict]:
        return []

    def save_rules(self, _: list[dict]) -> None:
        pass
