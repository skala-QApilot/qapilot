"""도메인 지식 Tool — 문서 파싱·청킹·민감정보 마스킹.

PDF·DOCX 등은 docling, XLSX는 openpyxl로 파싱한다.
파싱 결과를 슬라이딩 윈도우 방식으로 청크로 분할하고
민감정보를 마스킹한다.

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from qapilot.shared.errors import ErrorCode, ToolExecutionError

_CHUNK_SIZE = 300     # 청크당 단어 수
_CHUNK_OVERLAP = 50   # 인접 청크 간 겹치는 단어 수

_SENSITIVE_PATTERNS = [
    r"\d{6}-\d{7}",                                        # 주민등록번호
    r"\d{3}-\d{3,4}-\d{4}",                                # 전화번호
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", # 이메일
    r"\b(?:\d[ \-]?){13,16}\b",                            # 카드번호
]


class DocumentParser:
    """문서 파일을 파싱하여 청크 목록으로 변환한다.

    역할: PDF·DOCX·XLSX 파싱, 섹션 분할, 슬라이딩 윈도우 청킹, 민감정보 마스킹
    입력: 파일 경로
    출력: list[dict] — chunk_id, source, section, text
    """

    def parse_and_chunk(self, file_path: Path) -> list[dict]:
        """문서를 파싱하고 민감정보 마스킹 후 청크로 분할한다.

        Args:
            file_path: 파싱할 파일 경로.

        Returns:
            list[dict]: chunk_id, source, section, text 키를 가진 청크 목록.

        Raises:
            ToolExecutionError: 문서 파싱에 실패한 경우 (TOOL_005).
        """
        try:
            if file_path.suffix.lower() in (".xlsx", ".xls"):
                blocks = self._parse_excel(file_path)
            else:
                blocks = self._parse_with_docling(file_path)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(
                ErrorCode.TOOL_005,
                f"문서 파싱 실패: {e}. 수동 입력은 add_rule을 사용하세요.",
            ) from e

        chunks = []
        for block in blocks:
            masked = self._mask_sensitive(block["text"])
            for chunk_text in self._split_text(masked):
                chunks.append(
                    {
                        "chunk_id": str(uuid.uuid4()),
                        "source": file_path.name,
                        "section": block.get("section", ""),
                        "text": chunk_text,
                    }
                )
        return chunks

    def _parse_with_docling(self, file_path: Path) -> list[dict]:
        """docling으로 PDF·DOCX 등 문서를 파싱하여 섹션 블록 목록을 반환한다.

        Args:
            file_path: 파싱할 파일 경로.

        Returns:
            list[dict]: text, section 키를 가진 섹션 블록 목록.
        """
        from docling.document_converter import DocumentConverter

        result = DocumentConverter().convert(str(file_path))
        markdown = result.document.export_to_markdown()
        return self._split_markdown_sections(markdown, file_path.stem)

    def _parse_excel(self, file_path: Path) -> list[dict]:
        """openpyxl로 XLSX 파일을 파싱하여 시트별 블록 목록을 반환한다.

        Args:
            file_path: 파싱할 Excel 파일 경로.

        Returns:
            list[dict]: text(시트 전체 행), section(시트명) 키를 가진 블록 목록.
        """
        import openpyxl

        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        blocks = []
        for sheet_name in wb.sheetnames:
            rows = [
                "\t".join(str(c) for c in row if c is not None)
                for row in wb[sheet_name].iter_rows(values_only=True)
            ]
            text = "\n".join(r for r in rows if r.strip())
            if text:
                blocks.append({"text": text, "section": sheet_name})
        wb.close()
        return blocks

    def _split_markdown_sections(self, markdown: str, default_name: str) -> list[dict]:
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

    def _split_text(self, text: str) -> list[str]:
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
            chunks.append(" ".join(words[i : i + _CHUNK_SIZE]))
            i += _CHUNK_SIZE - _CHUNK_OVERLAP
        return chunks

    def _mask_sensitive(self, text: str) -> str:
        """주민번호·전화번호·이메일·카드번호 패턴을 '***'으로 마스킹한다.

        Args:
            text: 마스킹을 적용할 원본 텍스트.

        Returns:
            str: 민감정보가 마스킹된 텍스트.
        """
        for pattern in _SENSITIVE_PATTERNS:
            text = re.sub(pattern, "***", text)
        return text
