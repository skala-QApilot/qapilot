"""파이프라인 진행률 이벤트 — `run:<trace_id>` Redis 채널로 `progress` 이벤트 발행.

시나리오 생성(Layer 1A) / 코드 생성(Layer 1B) 오버레이의 프로그레스바를 실제 진행과
동기화하기 위한 전용 이벤트. 기존 status/annotate/tc_result 와 같은 envelope 를 쓰며,
Spring `RunService.stream` 이 그대로 forward → 프런트엔드 `useRunStream` 이 수신한다.

해상도:
  - 노드 진입 (`node`)  → 해당 노드 구간 시작 %.
  - 루프 항목 (`item`)  → 노드 구간 내부를 done/total 로 보간 (LLM 노드 2곳).

가드레일: 정수 % 가 올라갈 때만 발행한다. 따라서 항목 수가 수백~수천이어도 한 trace 당
발행 이벤트는 100개를 넘지 않으며, 표시 진행률은 항상 단조 증가한다.

Redis 미설정 시 publish 는 no-op — 프런트엔드는 타이머 fallback 으로 동작한다.
"""

from __future__ import annotations

from qapilot.messaging.redis_pubsub import publish_run_event

# 노드 → (시작%, 끝%) 구간. 끝% 는 heavy 노드의 항목 보간 상한.
# LLM 호출이 지배적인 scenario_generate / code_generate 에 가장 넓은 구간을 배정한다.
_BANDS: dict[str, tuple[int, int]] = {
    # Layer 1A — 시나리오 생성
    "doc_import": (0, 8),
    "codebase_scan": (8, 20),
    "domain_knowledge": (20, 30),
    "requirement_extract": (30, 45),
    "scenario_generate": (45, 92),
    "save_scenarios": (95, 99),
    # Layer 1B — 코드 생성
    "load_scenarios_for_codegen": (0, 10),
    "action_mapping": (10, 30),
    "code_generate": (30, 92),
    "save_codes": (95, 99),
}

_LABELS: dict[str, str] = {
    "doc_import": "문서를 임베딩하는 중...",
    "codebase_scan": "코드베이스를 분석하는 중...",
    "domain_knowledge": "도메인 지식을 불러오는 중...",
    "requirement_extract": "요구사항을 추출하는 중...",
    "scenario_generate": "시나리오를 생성하는 중...",
    "save_scenarios": "시나리오를 저장하는 중...",
    "load_scenarios_for_codegen": "시나리오를 불러오는 중...",
    "action_mapping": "액션 매핑을 작성하는 중...",
    "code_generate": "테스트 코드를 생성하는 중...",
    "save_codes": "코드를 저장하는 중...",
}

# 가드레일 상태 — trace_id → 마지막으로 발행한 정수 %. 단조 증가 보장.
_last_pct: dict[str, int] = {}


def _emit(trace_id: str | None, node: str, pct: int, message: str) -> None:
    """정수 % 가 직전보다 클 때만 progress 이벤트 1건 발행 (가드레일)."""
    if not trace_id or node not in _BANDS:
        return
    pct = max(0, min(100, pct))
    if pct <= _last_pct.get(trace_id, -1):
        return
    _last_pct[trace_id] = pct
    publish_run_event(trace_id, "progress", {"percent": pct, "node": node, "message": message})


def node(trace_id: str | None, name: str) -> None:
    """노드 진입 — 해당 노드 구간의 시작 % 로 진행률을 올린다."""
    band = _BANDS.get(name)
    if band:
        _emit(trace_id, name, band[0], _LABELS.get(name, "처리 중..."))


def item(trace_id: str | None, name: str, done: int, total: int) -> None:
    """루프 항목 완료 — 노드 구간 내부를 done/total 로 보간한다."""
    band = _BANDS.get(name)
    if not band or total <= 0:
        return
    start, end = band
    pct = start + int((end - start) * done / total)
    label = _LABELS.get(name, "처리 중...")
    _emit(trace_id, name, pct, f"{label} ({min(done, total)}/{total})")


def reset(trace_id: str | None) -> None:
    """trace 종료 시 가드레일 상태 정리 — 메모리 누적 방지."""
    if trace_id:
        _last_pct.pop(trace_id, None)
