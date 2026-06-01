"""증적 캡처/보관 모듈.

모든 TC의 실행 결과를 성공/실패 무관하게
trace_id 기준으로 영구 보관한다.

담당: E
Created: 2026-06-01
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


EVIDENCE_BASE_DIR = Path(os.getenv("EVIDENCE_BASE_DIR", "/tmp/qapilot/evidence"))


class EvidenceModule:
    """증적 보관 모듈.

    저장 위치: {EVIDENCE_BASE_DIR}/{trace_id}/{tc_id}/
    보관 항목: 스크린샷, API 요청/응답, SQL 로그, 콘솔 로그
    보존 기간: 24개월
    """

    def _evidence_path(self, trace_id: str, tc_id: str) -> Path:
        """증적 저장 경로 반환."""
        return EVIDENCE_BASE_DIR / trace_id / tc_id

    async def save(self, trace_id: str, tc_id: str, evidence: dict) -> None:
        """증적을 저장한다.

        Args:
            trace_id: 실행 trace ID
            tc_id: 테스트 케이스 ID
            evidence: 저장할 증적 데이터
                - screenshots: list[str] - 스크린샷 파일 경로
                - api_logs: list[dict] - API 요청/응답 로그
                - sql_logs: list[dict] - SQL 로그
                - console_logs: list[str] - 콘솔 로그
        """
        path = self._evidence_path(trace_id, tc_id)
        path.mkdir(parents=True, exist_ok=True)

        evidence_data = {
            "trace_id": trace_id,
            "tc_id": tc_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            **evidence,
        }

        evidence_file = path / "evidence.json"
        evidence_file.write_text(
            json.dumps(evidence_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def get(self, trace_id: str, tc_id: str) -> dict | None:
        """증적을 조회한다.

        Args:
            trace_id: 실행 trace ID
            tc_id: 테스트 케이스 ID

        Returns:
            저장된 증적 데이터 또는 None (없는 경우)
        """
        evidence_file = self._evidence_path(trace_id, tc_id) / "evidence.json"

        if not evidence_file.exists():
            return None

        return json.loads(evidence_file.read_text(encoding="utf-8"))