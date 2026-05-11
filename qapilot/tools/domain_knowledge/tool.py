"""도메인 지식 Tool (RAG).

PRD, 약관, 정책서 등 도메인 산출물을 벡터 DB에 임베딩하고
자연어 규칙을 축적한다. 시나리오 생성 시 RAG로 주입.

세부 구현은 하위 모듈에 위임한다.
    _parser.py   — 문서 파싱·청킹·마스킹
    _store.py    — Qdrant 임베딩·검색
    _glossary.py — 용어사전·규칙 파일 관리

Author: 전아린
Created: 2026-05-07
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from qapilot.shared.errors import ErrorCode, ToolExecutionError
from qapilot.tools.base_tool import BaseTool
from qapilot.tools.domain_knowledge._glossary import GlossaryManager
from qapilot.tools.domain_knowledge._parser import DocumentParser
from qapilot.tools.domain_knowledge._store import VectorStore


class DomainKnowledgeTool(BaseTool):
    """도메인 지식 Tool.

    역할: 문서 임베딩, Qdrant 벡터 검색, 용어사전(glossary.json) 관리
    입력 params["action"]:
        - "import"       : file_path (str)
        - "search"       : query (str), top_k (int, optional)
        - "add_rule"     : rule (str), category (str, optional)
        - "add_glossary" : code_name (str), business_name (str)
    출력: action별 결과 dict
    저장: Qdrant 컬렉션 + .qapilot/domain/
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """DomainKnowledgeTool을 초기화한다."""
        super().__init__(*args, **kwargs)
        self._parser = DocumentParser()
        self._store = VectorStore(logger=self.logger)
        self._glossary = GlossaryManager()

    async def _execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """action 파라미터에 따라 적절한 핸들러를 호출한다.

        Args:
            params: 실행 파라미터. params["action"]으로 동작을 결정한다.

        Returns:
            action별 결과 dict.

        Raises:
            ToolExecutionError: 알 수 없는 action이 전달된 경우 (TOOL_001).
        """
        action = params.get("action")
        if action == "import":
            return await self._import_document(params)
        if action == "search":
            return await self._search(params)
        if action == "add_rule":
            return await self._add_rule(params)
        if action == "add_glossary":
            return await self._add_glossary(params)
        raise ToolExecutionError(
            ErrorCode.TOOL_001,
            f"알 수 없는 action: {action!r}. import / search / add_rule / add_glossary 중 하나를 사용하세요.",
        )

    async def _import_document(self, params: dict) -> dict:
        """문서 파일을 파싱·청킹하여 Qdrant에 임베딩한다.

        Args:
            params: file_path (str) — 임포트할 문서의 경로.

        Returns:
            dict: file, chunks_total, chunks_stored, chunks_failed 포함.

        Raises:
            ToolExecutionError: 파일이 존재하지 않는 경우 (TOOL_002).
        """
        file_path = Path(params.get("file_path", ""))
        if not file_path.exists():
            raise ToolExecutionError(ErrorCode.TOOL_002, f"파일 없음: {file_path}")

        self.logger.info("doc_import_start", file=str(file_path))
        chunks = self._parser.parse_and_chunk(file_path)
        stored, failed = await self._store.embed_and_store(chunks)
        self._store.save_index(file_path, len(chunks))
        self.logger.info("doc_import_done", stored=stored, failed=failed)

        return {
            "file": str(file_path),
            "chunks_total": len(chunks),
            "chunks_stored": stored,
            "chunks_failed": failed,
        }

    async def _search(self, params: dict) -> dict:
        """쿼리를 임베딩하여 Qdrant에서 유사 도메인 규칙을 검색한다.

        glossary.json으로 코드명을 업무명으로 치환한 뒤 검색에 사용한다.

        Args:
            params: 검색 파라미터.
                query (str): 검색 쿼리.
                top_k (int, optional): 반환할 최대 결과 수. 기본값 5.

        Returns:
            dict: query, expanded_query, rules(List[DomainRule]) 포함.

        Raises:
            ToolExecutionError: query가 비어 있는 경우 (TOOL_001).
        """
        query = params.get("query", "")
        if not query:
            raise ToolExecutionError(ErrorCode.TOOL_001, "search에는 query 파라미터가 필요합니다.")

        expanded = self._glossary.expand(query)
        rules = await self._store.search(query=expanded, top_k=int(params.get("top_k", 5)))
        return {"query": query, "expanded_query": expanded, "rules": rules}

    async def _add_rule(self, params: dict) -> dict:
        """자연어 규칙을 Qdrant에 임베딩하고 rules.json에 저장한다.

        Args:
            params: 규칙 파라미터.
                rule (str): 추가할 자연어 규칙 문장.
                category (str, optional): 규칙 카테고리. 기본값 "custom_rule".

        Returns:
            dict: rule_id, category, content 포함.

        Raises:
            ToolExecutionError: rule이 비어 있는 경우 (TOOL_001).
        """
        rule_text = params.get("rule", "")
        category = params.get("category", "custom_rule")
        if not rule_text:
            raise ToolExecutionError(ErrorCode.TOOL_001, "add_rule에는 rule 파라미터가 필요합니다.")

        rule_id = str(uuid.uuid4())
        await self._store.upsert_point(
            point_id=rule_id,
            text=rule_text,
            payload={"source": "manual_rule", "section": "", "text": rule_text, "category": category},
        )

        rules = self._glossary.load_rules()
        rules.append({"rule_id": rule_id, "category": category, "content": rule_text})
        self._glossary.save_rules(rules)

        self.logger.info("rule_added", rule_id=rule_id, category=category)
        return {"rule_id": rule_id, "category": category, "content": rule_text}

    async def _add_glossary(self, params: dict) -> dict:
        """코드명↔업무명 매핑을 glossary.json에 추가한다.

        Args:
            params: 용어사전 파라미터.
                code_name (str): 시스템 코드명 (예: "usr_seq").
                business_name (str): 업무 용어 (예: "회원 일련번호").

        Returns:
            dict: code_name, business_name, total_entries 포함.

        Raises:
            ToolExecutionError: code_name 또는 business_name이 비어 있는 경우 (TOOL_001).
        """
        code_name = params.get("code_name", "")
        business_name = params.get("business_name", "")
        if not code_name or not business_name:
            raise ToolExecutionError(
                ErrorCode.TOOL_001,
                "add_glossary에는 code_name과 business_name이 필요합니다.",
            )

        glossary = self._glossary.load_glossary()
        glossary[code_name] = business_name
        self._glossary.save_glossary(glossary)

        self.logger.info("glossary_updated", code=code_name, business=business_name)
        return {"code_name": code_name, "business_name": business_name, "total_entries": len(glossary)}
