"""도메인 지식 Tool — 용어사전·규칙 파일 관리.

glossary.json(코드명↔업무명 매핑)과 rules.json(자연어 규칙)의
읽기·쓰기를 담당한다.

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import json
from pathlib import Path

_DOMAIN_DIR = Path(".qapilot/domain")
_GLOSSARY_PATH = _DOMAIN_DIR / "glossary.json"
_RULES_PATH = _DOMAIN_DIR / "rules.json"


class GlossaryManager:
    """glossary.json과 rules.json 파일 입출력을 담당한다.

    역할: 용어사전 로드·저장, 규칙 로드·저장, 쿼리 내 코드명 치환
    입력: dict 또는 list
    출력: dict[str, str] 또는 list[dict]
    저장: .qapilot/domain/glossary.json, .qapilot/domain/rules.json
    """

    def load_glossary(self) -> dict[str, str]:
        """glossary.json을 로드한다.

        Returns:
            dict[str, str]: 코드명→업무명 매핑. 파일이 없으면 빈 dict.
        """
        if _GLOSSARY_PATH.exists():
            return json.loads(_GLOSSARY_PATH.read_text())
        return {}

    def save_glossary(self, glossary: dict[str, str]) -> None:
        """glossary.json을 저장한다.

        Args:
            glossary: 코드명→업무명 매핑 dict.
        """
        _DOMAIN_DIR.mkdir(parents=True, exist_ok=True)
        _GLOSSARY_PATH.write_text(json.dumps(glossary, ensure_ascii=False, indent=2))

    def expand(self, text: str) -> str:
        """glossary.json의 코드명→업무명 매핑을 텍스트에 적용한다.

        Args:
            text: 치환을 적용할 원본 텍스트.

        Returns:
            str: 코드명이 업무명으로 치환된 텍스트.
        """
        for code, business in self.load_glossary().items():
            text = text.replace(code, business)
        return text

    def load_rules(self) -> list[dict]:
        """rules.json을 로드한다.

        Returns:
            list[dict]: 저장된 규칙 목록. 파일이 없으면 빈 리스트.
        """
        if _RULES_PATH.exists():
            return json.loads(_RULES_PATH.read_text())
        return []

    def save_rules(self, rules: list[dict]) -> None:
        """rules.json을 저장한다.

        Args:
            rules: 저장할 규칙 목록.
        """
        _DOMAIN_DIR.mkdir(parents=True, exist_ok=True)
        _RULES_PATH.write_text(json.dumps(rules, ensure_ascii=False, indent=2))
