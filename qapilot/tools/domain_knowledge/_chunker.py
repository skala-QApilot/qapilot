"""도메인 지식 Tool — 텍스트 청킹·민감정보 마스킹.

슬라이딩 윈도우 청킹과 민감정보 마스킹을 제공하는 순수 함수 모듈.
파일 파싱 로직과 분리하여 독립적으로 테스트·재사용할 수 있다.

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import re

CHUNK_SIZE = 300     # 청크당 단어 수
CHUNK_OVERLAP = 50   # 인접 청크 간 겹치는 단어 수

_SENSITIVE_PATTERNS = [
    r"\d{6}-\d{7}",                                        # 주민등록번호
    r"\d{3}-\d{3,4}-\d{4}",                                # 전화번호
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", # 이메일
    r"\b(?:\d[ \-]?){13,16}\b",                            # 카드번호
]


def split_markdown_sections(markdown: str, default_name: str) -> list[dict]:
    """마크다운을 헤더(#) 기준으로 섹션 블록 목록으로 분할한다.

    Args:
        markdown: 분할할 마크다운 문자열.
        default_name: 헤더가 없는 경우 사용할 기본 섹션명.

    Returns:
        list[dict]: text, section 키를 가진 섹션 블록 목록.
    """
    blocks: list[dict] = []
    current_section = default_name
    current_lines: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("#"):
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    blocks.append({"text": text, "section": current_section})
                current_lines = []
            current_section = line.lstrip("#").strip() or default_name
        else:
            current_lines.append(line)

    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            blocks.append({"text": text, "section": current_section})

    return blocks


def split_text(text: str) -> list[str]:
    """텍스트를 슬라이딩 윈도우 방식으로 단어 단위 청크로 분할한다.

    Args:
        text: 분할할 텍스트.

    Returns:
        list[str]: 분할된 청크 목록. 빈 텍스트이면 빈 리스트를 반환한다.
    """
    words = text.split()
    if not words:
        return []
    chunks = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + CHUNK_SIZE]))
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def mask_sensitive(text: str) -> str:
    """주민번호·전화번호·이메일·카드번호 패턴을 '***'으로 마스킹한다.

    Args:
        text: 마스킹을 적용할 원본 텍스트.

    Returns:
        str: 민감정보가 마스킹된 텍스트.
    """
    for pattern in _SENSITIVE_PATTERNS:
        text = re.sub(pattern, "***", text)
    return text
