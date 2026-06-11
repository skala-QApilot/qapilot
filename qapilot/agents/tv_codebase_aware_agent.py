"""TC 내용(given/when/then/value) 통합 생성 Agent — 문서+코드베이스+DB 기반.

정합성(coherence) 설계: given/when/then 과 value 를 **한 컨텍스트에서 함께** 생성한다.
앞 단계(TCFromDocsAgent)는 TC 골격(name/intent/technique/tags/api/req_id)만 열거하고,
본 Agent 가 검색 문서 + 코드베이스 메타데이터/본문 + DB 스냅샷을 모두 보고
given/when/then 과 value 를 동시에 만든다 → gwt 와 value 가 서로 모순되지 않는다.

입력으로:
- TC 골격 1개 (name/intent/technique/tags/api/req_id)
- 본 TS 의 검색 문서 (retrieved_docs)
- 본 TC 관련 메타데이터 (필터링된 schemas/selectors/routes/patterns)
- 본 TC 관련 DB snapshot (sensitive 컬럼 제외) + production 코드 본문 (source_snippets)

LLM 호출 → TVValidator 로 value 검증 → invalid 면 한두 번 재시도.

scenario_intent 는 골격의 `intent` 필드를 그대로 사용한다 (then 키워드 재추론 대신).

sensitive 필드 처리 (Option α — PR #256 본질 재발 방지):
- sensitive 필드는 LLM 컨텍스트에서 완전 제외
- TC.values 에는 placeholder (process.env.TEST_PASSWORD 등) 로 저장
- 액션매핑 단계의 PR #256 fallback 으로 실행 시 실제 값 주입
"""

from __future__ import annotations

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.metadata_filters import filter_metadata_for_tc
from qapilot.shared.schemas import ExecuteResult
from qapilot.shared.sensitive_mask import (
    build_sensitive_value_entries,
    get_sensitive_field_names,
    get_sensitive_request_field_names,
    merge_values_with_sensitive,
    strip_sensitive_from_db_snapshot,
    strip_sensitive_from_schemas,
)
from qapilot.shared.tv_validator import TVValidator, ValidationResult

_MAX_RETRY = 2


# ────────────────────────────────────────────────────────────────────────
# format helpers
# ────────────────────────────────────────────────────────────────────────

def _format_dict_compact(d: Any, *, indent: int = 2, max_chars: int = 4000) -> str:
    """LLM context 용 — dict / list 를 JSON 으로 압축. truncation 표시."""
    try:
        text = json.dumps(d, ensure_ascii=False, indent=indent, default=str)
    except (TypeError, ValueError):
        text = str(d)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text


def _format_source_snippets(snippets: list[dict] | None) -> str:
    """load_source 결과를 LLM prompt 친화 format 으로.

    각 snippet 마다 file path / line range 헤더 + 본문.
    총 길이 cap (12000 chars) — 너무 큰 경우 truncate.
    """
    if not snippets:
        return "코드베이스 본문 없음 (load_source 결과 비어 있음)"
    parts: list[str] = []
    total = 0
    cap = 12000
    for s in snippets:
        file = s.get("file", "?")
        ls = s.get("line_start")
        le = s.get("line_end")
        content = s.get("content") or ""
        # 헤더 + 본문
        range_str = f"L{ls}-{le}" if ls and le else "전체"
        header = f"\n[{file} ({range_str})]"
        body = content[:6000]  # 단일 snippet 6000자 cap
        if len(content) > 6000:
            body = body + "\n... (truncated)"
        block = header + "\n```\n" + body + "\n```"
        if total + len(block) > cap:
            parts.append("\n... (남은 snippet 생략 — token cap)")
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


def _format_retrieved_docs(docs: list[dict] | None) -> str:
    """TS 검색 문서를 LLM prompt 친화 format 으로 (given/when/then 근거)."""
    if not docs:
        return "검색된 문서 없음 (요구사항/스키마 기반으로 작성)"
    parts: list[str] = []
    for i, d in enumerate(docs, start=1):
        if not isinstance(d, dict):
            continue
        src = d.get("source", "")
        score = d.get("score", 0.0)
        content = (d.get("content", "") or "")[:2000]
        parts.append(f"[문서 {i}] 출처: {src} (유사도: {score:.2f})\n{content}")
    return "\n\n---\n\n".join(parts) if parts else "검색된 문서 없음"


def _format_db_snapshot(snapshot: dict | None) -> str:
    if not snapshot:
        return "DB snapshot 없음"
    rows = snapshot.get("rows") or []
    if not rows:
        return f"테이블 '{snapshot.get('table', '?')}' — 0 rows"
    return _format_dict_compact(
        {"table": snapshot.get("table"), "rows": rows[:3], "total_rows": len(rows)},
        max_chars=1500,
    )


def _extract_json(text: str) -> str:
    """LLM 응답에서 JSON 블록 추출 (markdown ```json ... ``` 대응)."""
    text = text.strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            text = m.group(1).strip()
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


# ────────────────────────────────────────────────────────────────────────
# scenario_intent 추출 — TVValidator 의 db_existence/absence 판정용
# ────────────────────────────────────────────────────────────────────────

# "이미 가입" / "존재" / "duplicate" / "409" / "Conflict" → expects_existing
_EXISTING_HINTS = ("이미", "존재", "duplicate", "409", "conflict", "있는", "기존")
# "신규" / "없는" / "new" / "201" / "Created" → expects_absent
_ABSENT_HINTS = ("신규", "없는", "new", "201", "created", "처음", "최초")


def _infer_scenario_intent(tc: dict) -> dict:
    """TC 의 then/tags 로 DB 존재성/부재성 추론.

    TVValidator 의 expects_existing_in_db / expects_absent_in_db 에 매핑.
    """
    then_text = (tc.get("then") or "").lower()
    tags = tc.get("tags") or []
    intent: dict[str, Any] = {}

    has_edge = "edge_case" in tags

    if has_edge and any(h in then_text for h in _EXISTING_HINTS):
        intent["expects_existing_in_db"] = True
    elif any(h in then_text for h in _ABSENT_HINTS):
        intent["expects_absent_in_db"] = True

    return intent


def _resolve_scenario_intent(tc: dict) -> dict:
    """골격의 `intent` 필드를 TVValidator 의도 dict 로 변환.

    TCFromDocsAgent 가 명시한 `intent` 를 우선 사용한다 (then 키워드 재추론 제거).
    intent 가 비어 있으면(레거시 TC) then/tags 기반 _infer_scenario_intent 로 fallback.
    """
    explicit = (tc.get("intent") or "").strip()
    if explicit == "expects_existing":
        return {"expects_existing_in_db": True}
    if explicit == "expects_absent":
        return {"expects_absent_in_db": True}
    if not explicit:
        return _infer_scenario_intent(tc)
    # normal / boundary / auth / expects_validation_error → DB 존재성 제약 없음
    return {}


def _pick_schema_name(schemas: dict[str, Any]) -> str | None:
    """필터링된 schemas 에서 검증 대상 schema 이름 선택 — request 우선."""
    req = schemas.get("request_schemas") or {}
    if req:
        return next(iter(req.keys()))
    return None


# ────────────────────────────────────────────────────────────────────────
# Agent 본체
# ────────────────────────────────────────────────────────────────────────

class TVFromCodebaseAgent(BaseAgent):
    """TC + 코드베이스/메타데이터/DB → TC.values 채우기.

    호출자 (pipeline._tv_generate_codebase_aware) 가 TC 1개씩 본 agent 에 넘긴다.
    """

    agent_name = "tv_codebase_aware"
    use_deep_model = True

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        tc: dict = context.get("tc") or {}
        # TS 검색 문서 — given/when/then 작성 근거 (정합성: gwt 와 value 를 같은 컨텍스트에서)
        retrieved_docs: list[dict] = context.get("retrieved_docs") or []
        # 4영역 메타데이터 raw (호출자가 load_metadata_index 으로 받아서 그대로 전달)
        selectors_raw = context.get("selectors")
        routes_raw = context.get("routes")
        schemas_raw = context.get("schemas")
        patterns_raw = context.get("patterns")
        db_snapshot = context.get("db_snapshot")  # {"table": ..., "rows": [...]} 또는 None
        # 코드베이스 본문 (load_source 결과) — [{file, line_start, line_end, content}, ...]
        source_snippets: list[dict] = context.get("source_snippets") or []

        # ── 1. TC 기반 필터링 ──────────────────────────────────────────
        filtered = filter_metadata_for_tc(
            tc,
            selectors=selectors_raw,
            routes=routes_raw,
            schemas=schemas_raw,
            patterns=patterns_raw,
        )

        # ── 2. sensitive 필드 식별 + LLM 컨텍스트에서 제외 ─────────────
        # 마스킹(LLM/DB 스냅샷에서 가리기)용 — request + db 컬럼 전체
        sensitive_names = get_sensitive_field_names(filtered["schemas"])
        # placeholder 주입용 — request 입력 필드만 (db 컬럼 password_hash/token 제외).
        # 폼에 없는 DB 컬럼을 모든 TC.values 에 박는 오염 방지.
        input_sensitive_names = get_sensitive_request_field_names(filtered["schemas"])
        type_hint_map = _build_type_hint_map(filtered["schemas"])
        sensitive_entries = build_sensitive_value_entries(
            input_sensitive_names, type_hint_map=type_hint_map,
        )

        sanitized_schemas = strip_sensitive_from_schemas(filtered["schemas"])
        sanitized_db_rows = strip_sensitive_from_db_snapshot(
            (db_snapshot or {}).get("rows") or [], sensitive_names,
        )
        sanitized_db_snapshot = (
            {"table": db_snapshot.get("table"), "rows": sanitized_db_rows}
            if db_snapshot else None
        )

        # ── 3. LLM 호출 + 재시도 흐름 ──────────────────────────────────
        validator = TVValidator()
        scenario_intent = _resolve_scenario_intent(tc)  # 골격 intent 우선 (재추론 제거)
        schema_name = _pick_schema_name(sanitized_schemas)

        last_validation: ValidationResult | None = None
        gwt: dict[str, str] = {"given": "", "when": "", "then": ""}
        llm_values: list[dict] = []
        confidence = 0.5

        for attempt in range(_MAX_RETRY + 1):
            feedback = self._build_feedback(last_validation)
            user_prompt = self.prompts.render(
                tc_name=str(tc.get("name", "")),
                tc_api=str(tc.get("api", "") or "(없음)"),
                tc_req_id=str(tc.get("req_id", "") or "(없음)"),
                tc_tags=", ".join(tc.get("tags") or []),
                tc_intent=str(tc.get("intent", "") or "normal"),
                tc_technique=str(tc.get("technique", "") or "(미지정)"),
                retrieved_docs=_format_retrieved_docs(retrieved_docs),
                schemas=_format_dict_compact(sanitized_schemas),
                selectors=_format_dict_compact(filtered["selectors"]),
                patterns=_format_dict_compact(filtered["patterns"], max_chars=1500),
                db_snapshot=_format_db_snapshot(sanitized_db_snapshot),
                source_snippets=_format_source_snippets(source_snippets),
                validation_feedback=feedback,
            )

            response = await self.llm.chat(
                system_prompt=self.prompts.system(),
                user_prompt=user_prompt,
            )

            parsed = self._parse(response.content)
            gwt = parsed["gwt"]
            llm_values = parsed["values"]
            confidence = parsed["confidence"]

            # 검증 — values 의 각 field 마다 TVValidator 호출
            last_validation = self._validate_values(
                llm_values, scenario_intent, sanitized_db_rows,
                sanitized_schemas, schema_name, validator,
            )
            if last_validation.valid:
                break

        # ── 4. sensitive 필드 placeholder 머지 ─────────────────────────
        merged_values = merge_values_with_sensitive(llm_values, sensitive_entries)

        return ExecuteResult(
            result={
                "given": gwt["given"],
                "when": gwt["when"],
                "then": gwt["then"],
                "values": merged_values,
                "validation_passed": (last_validation.valid if last_validation else False),
                "validation_reasons": (
                    last_validation.reasons if last_validation and not last_validation.valid else []
                ),
            },
            confidence=confidence,
        )

    # ── 내부 헬퍼 ──────────────────────────────────────────────────────

    def _parse(self, content: str) -> dict[str, Any]:
        empty_gwt = {"given": "", "when": "", "then": ""}
        try:
            cleaned = _extract_json(content)
            data = json.loads(cleaned)
        except Exception as e:
            self.logger.warning(
                "tv_codebase_aware_parse_error",
                error=str(e), raw=content[:300],
            )
            return {"gwt": empty_gwt, "values": [], "confidence": 0.0}

        gwt = {
            "given": str(data.get("given", "") or ""),
            "when": str(data.get("when", "") or ""),
            "then": str(data.get("then", "") or ""),
        }

        raw_values = data.get("values") or []
        valid: list[dict] = []
        for v in raw_values:
            if not isinstance(v, dict) or not v.get("field"):
                continue
            valid.append({
                "field": str(v.get("field", "")),
                "value": v.get("value"),
                "type": str(v.get("type", "string")),
                "purpose": str(v.get("purpose", "")),
                "source": str(v.get("source", "llm")),
            })
        return {"gwt": gwt, "values": valid, "confidence": float(data.get("confidence", 0.7))}

    def _validate_values(
        self,
        values: list[dict],
        intent: dict,
        db_rows: list[dict],
        schemas: dict,
        schema_name: str | None,
        validator: TVValidator,
    ) -> ValidationResult:
        """각 value 마다 TVValidator 호출 — 모두 valid 면 valid=True."""
        if not values:
            return ValidationResult(
                valid=False,
                checks=[],
                reasons=["values 가 비어 있음"],
            )

        all_reasons: list[str] = []
        for v in values:
            result = validator.validate(
                tv_field={"name": v.get("field"), "value": v.get("value")},
                scenario_intent=intent,
                db_snapshot=db_rows,
                schemas=schemas,
                schema_name=schema_name,
            )
            if not result.valid:
                all_reasons.extend(f"[{v.get('field')}] {r}" for r in result.reasons)

        valid = len(all_reasons) == 0
        return ValidationResult(valid=valid, checks=[], reasons=all_reasons)

    def _build_feedback(self, prev_result: ValidationResult | None) -> str:
        if prev_result is None or prev_result.valid:
            return "(첫 시도 — 피드백 없음)"
        return "이전 시도가 다음 사유로 실패했다. 같은 실수 반복하지 마라:\n" + "\n".join(
            f"- {r}" for r in prev_result.reasons[:10]
        )


# ────────────────────────────────────────────────────────────────────────
# helpers (외부에서도 사용 가능)
# ────────────────────────────────────────────────────────────────────────

def _build_type_hint_map(schemas: dict[str, Any]) -> dict[str, str]:
    """필터된 schemas 에서 field → type 매핑 추출 (sensitive placeholder 타입 hint 용)."""
    out: dict[str, str] = {}
    for spec in (schemas.get("request_schemas") or {}).values():
        for f in spec.get("fields") or []:
            name = f.get("name")
            ftype = f.get("type")
            if name and ftype:
                out[name] = ftype
    for model in (schemas.get("db_models") or {}).values():
        for col in model.get("columns") or []:
            name = col.get("name")
            ctype = col.get("type")
            if name and ctype and name not in out:
                out[name] = ctype
    return out
