"""TestPatternRecord 의 LLM 의미 분류 — PoC 5.1 (데이터 layer).

AST 단계 (pytest_ast_parser) 에서 'unknown' 으로 남은 record 를 LLM 으로 분류.
회의 결정 2 (2026-06-09): confidence=0.85 (LLM 의미라벨), extraction_method="hybrid".

분류 카테고리 (도메인 무관, framework 일반):
- db-seed         : DB row 생성/시드 (session.add / .commit / INSERT)
- assertion       : 단순 assertion 위주 (side-effect 없음)
- cleanup         : teardown / fixture finalizer
- wait-strategy   : sleep / wait_until / polling
- page-object     : Page object 패턴 (Playwright/Selenium)
- unknown         : 위 어느 것도 아님 (LLM 도 판단 불능)

이미 AST 가 분류한 (fixture/auth-setup/mock) 는 호출하지 않음.

batch 정책:
- 10 records / LLM 호출 (token 안전 + 비용 절감)
- snippet 은 처음 600자만 노출 (token 절감, AST 가 이미 line range 알고 있음)

결정성:
- temperature=0.0 (LLMClient 기본값)
- model 고정 (config.default_model)
- seed 는 LangChain ChatOpenAI 가 지원하면 자동 ( LLMClient 의 책임)

graceful:
- LLMClient = None → records 그대로 반환 (분류 안 됨, 호출자 자유)
- LLM 호출 실패 → 해당 batch 의 records 그대로 (unknown 유지, 로그)

Author: 주환 (kimjuhwan).
Created: 2026-06-09
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from qapilot.shared.metadata_schemas import PatternKind, TestPatternRecord

logger = logging.getLogger(__name__)

# LLM 분류 대상 — AST 가 못 잡은 unknown 만
_LLM_TARGET_KIND = "unknown"

# LLM 이 선택할 수 있는 카테고리 (이미 AST 가 잡은 것 제외)
_LLM_ALLOWED_KINDS: tuple[PatternKind, ...] = (
    "db-seed", "assertion", "cleanup", "wait-strategy", "page-object", "unknown",
)

_BATCH_SIZE = 10
_SNIPPET_MAX_CHARS = 600
_LLM_CONFIDENCE = 0.85


_SYSTEM_PROMPT = """\
당신은 pytest 테스트 함수의 패턴을 분류하는 정적 분석가입니다.
다음 카테고리 중 정확히 하나로 분류하세요 (도메인 무관, framework 일반 패턴):

- db-seed       : DB row 생성/시드 (session.add, .commit, INSERT, fixture 가 데이터 만듦)
- assertion     : 단순 assert 위주, side-effect 없는 검증
- cleanup       : teardown 호출, fixture finalizer (yield 뒤 코드)
- wait-strategy : sleep, wait_until, polling, retry 패턴
- page-object   : Page object 패턴 (Playwright/Selenium 의 page.method 호출)
- unknown       : 위 어느 것도 명확히 매칭 안 됨

규칙:
1. 코드 snippet 만으로 판단. 함수 이름은 참고만 (오해 유발 가능).
2. 비즈니스 도메인 (요금제/주문/계약 등) 키워드는 무시. 패턴 자체만 봄.
3. 모호하면 "unknown" — 잘못된 분류보다 정직이 낫다.

응답은 반드시 JSON 배열 (다른 텍스트 X):
[{"index": 0, "pattern_kind": "db-seed", "purpose": "신규 customer 시드"},
 {"index": 1, "pattern_kind": "assertion", "purpose": "응답 status 검증"}]
"""


async def classify_unknown_patterns(
    records: Sequence[TestPatternRecord],
    *,
    llm_client: Any,  # qapilot.shared.llm_client.LLMClient 또는 None
    batch_size: int = _BATCH_SIZE,
) -> list[TestPatternRecord]:
    """records 중 'unknown' 인 것만 LLM 으로 분류 — 갱신된 새 list 반환.

    Args:
        records: TestPatternRecord 리스트.
        llm_client: LLMClient 인스턴스 또는 None (None 이면 records 그대로).
        batch_size: 한 LLM 호출에 묶을 record 수.

    Returns:
        새 list — unknown 이었던 record 가 LLM 분류 결과로 교체됨.
        분류 실패한 것은 unknown 유지.
        AST 가 이미 분류한 (fixture/auth-setup/mock 등) 은 손대지 않음.

    Side effects:
        llm_client 가 None 아니면 LLM 호출 (cost 발생).
    """
    if llm_client is None:
        logger.info("llm_classifier_disabled", extra={"reason": "no_llm_client"})
        return list(records)

    out: list[TestPatternRecord] = list(records)

    # unknown 만 indexing
    unknown_indices = [i for i, r in enumerate(out) if r.pattern_kind == _LLM_TARGET_KIND]
    if not unknown_indices:
        return out

    logger.info(
        "llm_classifier_start",
        extra={"total": len(out), "unknown": len(unknown_indices),
               "batch_size": batch_size},
    )

    for batch_start in range(0, len(unknown_indices), batch_size):
        batch_indices = unknown_indices[batch_start:batch_start + batch_size]
        batch_records = [out[i] for i in batch_indices]

        try:
            classified = await _classify_batch(batch_records, llm_client)
        except Exception as e:
            logger.warning(
                "llm_classifier_batch_failed",
                extra={"batch_start": batch_start, "error": str(e)},
            )
            continue

        # batch index → 전체 index
        for local_idx, new_kind_purpose in classified.items():
            if local_idx >= len(batch_indices):
                continue
            global_idx = batch_indices[local_idx]
            new_kind, new_purpose = new_kind_purpose
            if new_kind not in _LLM_ALLOWED_KINDS:
                logger.warning(
                    "llm_classifier_invalid_kind",
                    extra={"kind": new_kind, "global_idx": global_idx},
                )
                continue
            if new_kind == "unknown":
                continue  # LLM 도 판단 불능 — 갱신 안 함 (AST 결과 유지)
            out[global_idx] = _update_record(out[global_idx], new_kind, new_purpose)

    return out


async def _classify_batch(
    batch: Sequence[TestPatternRecord],
    llm_client: Any,
) -> dict[int, tuple[PatternKind, str]]:
    """한 batch 호출 — {local_idx: (pattern_kind, purpose)} 반환."""
    user_prompt = _build_user_prompt(batch)
    response = await llm_client.chat(
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=0.0,
    )
    return _parse_response(response.content)


def _build_user_prompt(batch: Sequence[TestPatternRecord]) -> str:
    """batch records → user prompt (index + 잘라낸 snippet)."""
    parts: list[str] = []
    for i, rec in enumerate(batch):
        snippet = rec.snippet
        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[:_SNIPPET_MAX_CHARS] + "\n... (truncated)"
        parts.append(f"[{i}] {rec.file}:{rec.line_start}\n```python\n{snippet}\n```")
    return "다음 pytest record 들을 분류:\n\n" + "\n\n".join(parts)


def _parse_response(content: str) -> dict[int, tuple[PatternKind, str]]:
    """LLM 응답 → {index: (kind, purpose)} 매핑. 파싱 실패 시 빈 dict."""
    # JSON 추출 — LLM 이 markdown ```json 으로 감싼 경우 대응
    text = content.strip()
    if text.startswith("```"):
        # 첫 줄 + 마지막 줄 제거 (```json ... ```)
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("llm_classifier_parse_failed", extra={"error": str(e),
                                                              "head": content[:200]})
        return {}

    if not isinstance(data, list):
        logger.warning("llm_classifier_unexpected_shape",
                       extra={"type": type(data).__name__})
        return {}

    out: dict[int, tuple[PatternKind, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        kind = entry.get("pattern_kind")
        purpose = entry.get("purpose", "")
        if not isinstance(idx, int) or not isinstance(kind, str):
            continue
        out[idx] = (kind, str(purpose))  # type: ignore[arg-type]
    return out


def _update_record(
    rec: TestPatternRecord,
    new_kind: PatternKind,
    new_purpose: str,
) -> TestPatternRecord:
    """기존 record → LLM 분류 결과로 교체된 새 record (immutable 패턴)."""
    payload = rec.model_dump()
    payload["pattern_kind"] = new_kind
    payload["purpose"] = new_purpose or rec.purpose
    payload["confidence"] = _LLM_CONFIDENCE
    payload["extraction_method"] = "hybrid"
    return TestPatternRecord.model_validate(payload)
