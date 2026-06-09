"""챗봇 대화 세션 저장소.

natural_lang 트리거의 멀티턴 대화를 지원하기 위해
.qapilot/sessions/{session_id}.json 에 교환 이력을 저장한다.

이슈 #182
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")


def _session_path(qapilot_dir: str | Path, session_id: str) -> Path:
    return Path(qapilot_dir) / "sessions" / f"{session_id}.json"


def _now_kst() -> str:
    return datetime.now(_KST).isoformat()


def new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:12]}"


def load_session(qapilot_dir: str | Path, session_id: str) -> dict:
    """세션을 로드한다. 없으면 빈 세션을 반환한다."""
    path = _session_path(qapilot_dir, session_id)
    if not path.exists():
        return {"session_id": session_id, "exchanges": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"session_id": session_id, "exchanges": []}


def save_exchange(
    qapilot_dir: str | Path,
    session_id: str,
    user_input: str,
    query_status: str,
    query_feedback: str | None = None,
) -> None:
    """교환 1건을 세션에 추가한다.

    Args:
        query_status: "sufficient" | "insufficient" | "rejected"
        query_feedback: insufficient 시 clarification_question,
                        rejected 시 rejection_reason.
    """
    path = _session_path(qapilot_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    session = load_session(qapilot_dir, session_id)
    session["exchanges"].append({
        "user": user_input,
        "query_status": query_status,
        "query_feedback": query_feedback,
        "timestamp": _now_kst(),
    })
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def get_last_insufficient_exchange(qapilot_dir: str | Path, session_id: str) -> dict | None:
    """직전 교환이 insufficient이면 해당 교환을 반환, 아니면 None."""
    session = load_session(qapilot_dir, session_id)
    exchanges = session.get("exchanges", [])
    if not exchanges:
        return None
    last = exchanges[-1]
    if last.get("query_status") == "insufficient":
        return last
    return None


def save_pending_requirements(
    qapilot_dir: str | Path,
    session_id: str,
    requirements: list[dict],
) -> None:
    """_similar_tc 감지로 중단된 요건을 세션에 저장한다.

    다음 턴에서 사용자가 '새로 추가'를 선택하면 이 요건을 재사용한다.
    """
    path = _session_path(qapilot_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    session = load_session(qapilot_dir, session_id)
    session["pending_requirements"] = requirements
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_pending_requirements(qapilot_dir: str | Path, session_id: str) -> list[dict] | None:
    """저장된 pending_requirements를 꺼내고 세션에서 제거한다."""
    path = _session_path(qapilot_dir, session_id)
    session = load_session(qapilot_dir, session_id)
    reqs = session.pop("pending_requirements", None)
    if reqs is not None:
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return reqs


def save_pending_similar_tc(
    qapilot_dir: str | Path,
    session_id: str,
    similar_tc: dict,
) -> None:
    """유사 TC 감지 시 tc_id·ts_id 좌표를 세션에 보존한다.

    다음 턴에서 사용자가 '수정'을 선택하면 이 정보로 target_tc_id를 확정한다.
    """
    path = _session_path(qapilot_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    session = load_session(qapilot_dir, session_id)
    session["pending_similar_tc"] = similar_tc
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def pop_pending_similar_tc(qapilot_dir: str | Path, session_id: str) -> dict | None:
    """저장된 pending_similar_tc를 꺼내고 세션에서 제거한다."""
    path = _session_path(qapilot_dir, session_id)
    session = load_session(qapilot_dir, session_id)
    info = session.pop("pending_similar_tc", None)
    if info is not None:
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def get_last_exchange(qapilot_dir: str | Path, session_id: str) -> dict | None:
    """직전 교환을 반환한다 (query_status 무관).

    sufficient 교환도 포함해 연속 요청("도", "도 추가해줘" 등)의 맥락 유지를 위해 사용.
    rejected 교환은 맥락 오염 방지를 위해 None 반환.
    """
    session = load_session(qapilot_dir, session_id)
    exchanges = session.get("exchanges", [])
    if not exchanges:
        return None
    last = exchanges[-1]
    if last.get("query_status") == "rejected":
        return None
    return last
