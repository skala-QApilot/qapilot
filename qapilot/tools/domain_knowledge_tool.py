"""도메인 지식 Tool (RAG).

PRD, 약관, 정책서 등 도메인 산출물을 벡터 DB에 임베딩하고
자연어 규칙을 축적한다. 시나리오 생성 시 RAG로 주입.

담당: B
Created: 2026-05-07
"""

from qapilot.shared.schemas import ToolInput, ToolOutput
from qapilot.tools.base_tool import BaseTool


class DomainKnowledgeTool(BaseTool):
    """도메인 지식 Tool.

    역할: 문서 임베딩, Qdrant 벡터 검색, 용어사전(glossary.json)
    입력: 문서 파일 경로 또는 검색 쿼리
    출력: List[DomainRule] (유사도 검색 결과)
    저장: Qdrant + .qapilot/domain/
    """

    async def run(self, input: ToolInput) -> ToolOutput:
        raise NotImplementedError
