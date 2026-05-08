"""코드베이스 스캔 Tool.

프로젝트 소스코드를 정적 분석하여 API 엔드포인트, 데이터 모델,
프레임워크, Git 이력을 추출하고 캐싱한다.

담당: B
Created: 2026-05-07
"""

from qapilot.tools.base_tool import BaseTool


class CodebaseScannerTool(BaseTool):
    """코드베이스 스캔 Tool.

    역할: AST 파싱, 엔드포인트/모델/종속성/callgraph 추출, 증분 재분석
    입력: 저장소 경로, Git diff
    출력: ScanResult (FileInfo, GitDiff, callgraph.json)
    저장: .qapilot/codebase-index/
    """

    async def _execute(self, params: dict) -> dict:
        raise NotImplementedError
