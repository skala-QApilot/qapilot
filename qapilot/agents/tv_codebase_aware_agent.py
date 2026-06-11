"""코드베이스/DB 기반 TV(Test Value) 채우기 Agent.

회의 4단계 워크플로우의 Step 4 — TC + 코드베이스/메타데이터/DB → TV.

TC 자체는 변경하지 않고 `values` 만 채운다. 입력으로:
- TC 1개 (name/api/req_id/given/when/then/기존 values)
- 본 TC 관련 메타데이터 (필터링된 schemas/selectors/routes/patterns)
- 본 TC 관련 DB snapshot (sensitive 컬럼 제외)

LLM 호출 → TVValidator 검증 → invalid 면 한두 번 재시도.

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


def _format_existing_values(values: list[dict]) -> str:
    if not values:
        return "없음 (placeholder 도 없음)"
    return _format_dict_compact(values, max_chars=1500)


def _codebase_evidence_refs(filtered: dict[str, Any]) -> list[dict[str, str]]:
    """필터링된 4영역 메타데이터에서 실제로 매칭된 항목들을 근거 ref 목록으로 변환.

    given/when/then/value 의 evidence_refs (provenance) 채우는 데 쓰인다.
    빈 리스트면 해당 TC 에 코드 근거가 없었다는 뜻 — 호출자가 "미해결" 처리.
    """
    refs: list[dict[str, str]] = []
    schemas = filtered.get("schemas") or {}
    for bucket in ("request_schemas", "response_schemas"):
        for name in (schemas.get(bucket) or {}):
            refs.append({"kind": "backend.schemas", "ref": name})
    for name in (schemas.get("db_models") or {}):
        refs.append({"kind": "backend.schemas", "ref": name})

    selectors = filtered.get("selectors") or {}
    for route in (selectors.get("by_route") or {}):
        refs.append({"kind": "frontend.selectors", "ref": route})

    seen_files: set[str] = set()
    patterns = filtered.get("patterns") or {}
    for p in (patterns.get("patterns") or []):
        file = p.get("file")
        if file and file not in seen_files:
            seen_files.add(file)
            refs.append({"kind": "sut_tests.patterns", "ref": file})

    return refs


def _evidence_ref_strings(refs: list[dict[str, str]]) -> list[str]:
    return [f"{r['kind']}:{r['ref']}" for r in refs]


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
# "400" / "Bad Request" / "유효하지 않" / "위반" → 의도적 형식 위반 (음성 테스트)
_FORMAT_VIOLATION_HINTS = (
    "400", "bad request", "유효하지 않", "형식이 올바르지", "위반", "invalid",
)
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _infer_scenario_intent(tc: dict) -> dict:
    """TC 의 then/tags 로 DB 존재성/부재성/형식 위반 의도 추론.

    TVValidator 의 expects_existing_in_db / expects_absent_in_db /
    expects_format_violation 에 매핑.
    """
    then_text = (tc.get("then") or "").lower()
    tags = tc.get("tags") or []
    intent: dict[str, Any] = {}

    has_edge = "edge_case" in tags or "boundary" in tags

    if has_edge and any(h in then_text for h in _EXISTING_HINTS):
        intent["expects_existing_in_db"] = True
    elif any(h in then_text for h in _ABSENT_HINTS):
        intent["expects_absent_in_db"] = True

    if has_edge and any(h in then_text for h in _FORMAT_VIOLATION_HINTS):
        intent["expects_format_violation"] = True

    return intent


def _pick_schema_name(schemas: dict[str, Any]) -> str | None:
    """필터링된 schemas 에서 검증 대상 schema 이름 선택 — request 우선."""
    req = schemas.get("request_schemas") or {}
    if req:
        return next(iter(req.keys()))
    return None


def _request_schema_fields(schemas: dict[str, Any]) -> list[dict[str, Any]]:
    req = schemas.get("request_schemas") or {}
    if not req:
        return []
    first = next(iter(req.values()))
    return list(first.get("fields") or [])


def _field_name_from_v_model(v_model: str | None) -> str | None:
    if not v_model:
        return None
    parts = [p for p in str(v_model).split(".") if p]
    return parts[-1] if parts else None


def _ui_input_field_names(selectors: dict[str, Any]) -> set[str]:
    by_route = selectors.get("by_route") or {}
    names: set[str] = set()
    for route in by_route.values():
        for item in route.get("inputs") or []:
            v_model_name = _field_name_from_v_model(item.get("v_model"))
            if v_model_name:
                names.add(v_model_name)
            testid = str(item.get("testid") or "").strip()
            if testid:
                names.add(testid)
    return names


def _selector_meta_by_field(selectors: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_route = selectors.get("by_route") or {}
    out: dict[str, dict[str, Any]] = {}
    for route in by_route.values():
        for item in route.get("inputs") or []:
            names = {
                n for n in (
                    _field_name_from_v_model(item.get("v_model")),
                    str(item.get("testid") or "").strip() or None,
                ) if n
            }
            for name in names:
                out[name] = item
    return out


def _normalize_ui_value_type(field_type: str) -> str:
    lowered = (field_type or "string").lower()
    if "bool" in lowered:
        return "boolean"
    if any(tok in lowered for tok in ("int", "integer")):
        return "integer"
    if any(tok in lowered for tok in ("float", "double", "decimal", "number")):
        return "number"
    if "date" in lowered:
        return "date"
    return "string"


def _default_ui_value(field_spec: dict[str, Any], selector_meta: dict[str, Any] | None) -> Any:
    examples = field_spec.get("examples") or []
    if examples:
        return examples[0]
    default = field_spec.get("default")
    if default is not None:
        return default

    name = str(field_spec.get("name") or "").lower()
    field_type = str(field_spec.get("type") or "string")
    normalized_type = _normalize_ui_value_type(field_type)
    html_type = str((selector_meta or {}).get("html_type") or "").lower()
    validators = field_spec.get("validators") or []
    validator_kinds = {str(v.get("kind") or "").lower() for v in validators if isinstance(v, dict)}

    if "email" in name or "email_format" in validator_kinds or html_type == "email":
        return "newuser_2026@test.com"
    if "birth" in name or normalized_type == "date" or html_type == "date":
        return "2000-01-01"
    if normalized_type == "boolean" or html_type == "checkbox":
        return True
    if "name" in name:
        return "홍길동"
    if any(tok in name for tok in ("phone", "mobile", "tel")):
        return "01012345678"
    if normalized_type == "integer":
        return 1
    if normalized_type == "number":
        return 1
    return "테스트값"


def _ui_input_values_only(
    llm_values: list[dict[str, Any]],
    *,
    existing_values: list[dict[str, Any]],
    schemas: dict[str, Any],
    selectors: dict[str, Any],
    sensitive_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    request_fields = _request_schema_fields(schemas)
    if not request_fields:
        return merge_values_with_sensitive(llm_values, sensitive_entries)

    ui_field_names = _ui_input_field_names(selectors)
    if ui_field_names:
        target_fields = [f for f in request_fields if str(f.get("name") or "") in ui_field_names]
    else:
        target_fields = request_fields

    target_field_names = {str(f.get("name") or "") for f in target_fields}
    selector_meta_map = _selector_meta_by_field(selectors)
    by_field_existing = {
        str(v.get("field") or ""): v
        for v in (existing_values or [])
        if isinstance(v, dict) and v.get("field")
    }
    by_field_llm = {
        str(v.get("field") or ""): v
        for v in (llm_values or [])
        if isinstance(v, dict) and v.get("field") in target_field_names
    }
    sensitive_by_field = {
        str(v.get("field") or ""): v
        for v in (sensitive_entries or [])
        if isinstance(v, dict) and v.get("field") in target_field_names
    }

    normalized: list[dict[str, Any]] = []
    for field_spec in target_fields:
        name = str(field_spec.get("name") or "")
        field_type = str(field_spec.get("type") or "string")
        if name in sensitive_by_field:
            normalized.append(sensitive_by_field[name])
            continue

        current = by_field_llm.get(name) or by_field_existing.get(name)
        if current is not None:
            normalized.append({**current, "field": name, "type": str(current.get("type") or field_type)})
            continue

        selector_meta = selector_meta_map.get(name)
        normalized.append({
            "field": name,
            "value": _default_ui_value(field_spec, selector_meta),
            "type": _normalize_ui_value_type(field_type),
            "purpose": "필수 UI 입력값 보강",
            "source": "ui_default",
            "status": "grounded_code",
            "evidence_refs": [],
            "unresolved": False,
        })

    return normalized


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
        # 4영역 메타데이터 raw (호출자가 load_metadata_index 으로 받아서 그대로 전달)
        selectors_raw = context.get("selectors")
        routes_raw = context.get("routes")
        schemas_raw = context.get("schemas")
        patterns_raw = context.get("patterns")
        db_snapshot = context.get("db_snapshot")  # {"table": ..., "rows": [...]} 또는 None
        # 코드베이스 본문 (load_source 결과) — [{file, line_start, line_end, content}, ...]
        source_snippets: list[dict] = context.get("source_snippets") or []
        # TC별 재검색(Option A) 문서 chunk — [{content, source, score}, ...]
        tc_doc_refs: list[dict] = context.get("tc_doc_refs") or []

        # ── 1. TC 기반 필터링 ──────────────────────────────────────────
        filtered = filter_metadata_for_tc(
            tc,
            selectors=selectors_raw,
            routes=routes_raw,
            schemas=schemas_raw,
            patterns=patterns_raw,
        )

        # ── 2. sensitive 필드 식별 + LLM 컨텍스트에서 제외 ─────────────
        request_field_names = {
            str(f.get("name") or "")
            for f in _request_schema_fields(filtered["schemas"])
            if f.get("name")
        }
        sensitive_names = get_sensitive_field_names(filtered["schemas"])
        ui_sensitive_names = sensitive_names & request_field_names
        type_hint_map = _build_type_hint_map(filtered["schemas"])
        sensitive_entries = build_sensitive_value_entries(
            ui_sensitive_names, type_hint_map=type_hint_map,
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
        scenario_intent = _infer_scenario_intent(tc)
        schema_name = _pick_schema_name(sanitized_schemas)

        last_validation: ValidationResult | None = None
        llm_values: list[dict] = []
        resolved_claims: dict[str, str] = {}
        unverified_claims: list[str] = []
        confidence = 0.5
        unresolved_claims = _collect_unresolved_claims(tc)

        for attempt in range(_MAX_RETRY + 1):
            feedback = self._build_feedback(last_validation)
            user_prompt = self.prompts.render(
                tc_name=str(tc.get("name", "")),
                tc_api=str(tc.get("api", "") or "(없음)"),
                tc_req_id=str(tc.get("req_id", "") or "(없음)"),
                tc_tags=", ".join(tc.get("tags") or []),
                tc_given=str(tc.get("given", "")),
                tc_when=str(tc.get("when", "")),
                tc_then=str(tc.get("then", "")),
                unresolved_claims=_format_unresolved_claims(unresolved_claims),
                existing_values=_format_existing_values(tc.get("values") or []),
                schemas=_format_dict_compact(sanitized_schemas),
                selectors=_format_dict_compact(filtered["selectors"]),
                patterns=_format_dict_compact(filtered["patterns"], max_chars=1500),
                db_snapshot=_format_db_snapshot(sanitized_db_snapshot),
                source_snippets=_format_source_snippets(source_snippets),
                tc_doc_refs=_format_tc_doc_refs(tc_doc_refs),
                validation_feedback=feedback,
            )

            response = await self.llm.chat(
                system_prompt=self.prompts.system(),
                user_prompt=user_prompt,
            )

            parsed = self._parse(response.content)
            llm_values = parsed["values"]
            resolved_claims = parsed["claims"]
            unverified_claims = parsed["unverified_claims"]
            confidence = parsed["confidence"]

            # 검증 — values 의 각 field 마다 TVValidator 호출
            last_validation = self._validate_values(
                llm_values, scenario_intent, sanitized_db_rows,
                sanitized_schemas, schema_name, validator,
            )
            if last_validation.valid:
                break

        # ── 4. sensitive 필드 placeholder 머지 ─────────────────────────
        merged_values = _ui_input_values_only(
            llm_values,
            existing_values=tc.get("values") or [],
            schemas=filtered["schemas"],
            selectors=filtered["selectors"],
            sensitive_entries=sensitive_entries,
        )

        values_valid = last_validation.valid if last_validation else False
        reasons = list(last_validation.reasons) if last_validation and not values_valid else []
        if unverified_claims:
            reasons += [f"미확인 주장(then 등): {c}" for c in unverified_claims]

        return ExecuteResult(
            result={
                "values": merged_values,
                "claims": resolved_claims,
                "unverified_claims": unverified_claims,
                "validation_passed": values_valid and not unverified_claims,
                "validation_reasons": reasons,
            },
            confidence=confidence,
        )

    # ── 내부 헬퍼 ──────────────────────────────────────────────────────

    def _parse(self, content: str) -> dict[str, Any]:
        try:
            cleaned = _extract_json(content)
            data = json.loads(cleaned)
        except Exception as e:
            self.logger.warning(
                "tv_codebase_aware_parse_error",
                error=str(e), raw=content[:300],
            )
            return {"values": [], "claims": {}, "unverified_claims": [], "confidence": 0.0}

        raw_values = data.get("values") or []
        raw_claims = data.get("claims") or {}
        raw_unverified = data.get("unverified_claims") or []
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
        claims: dict[str, str] = {}
        if isinstance(raw_claims, dict):
            for key in ("given", "when", "then"):
                if raw_claims.get(key) is not None:
                    claims[key] = str(raw_claims.get(key) or "")
        unverified_claims = [str(c) for c in raw_unverified if str(c).strip()]
        return {
            "values": valid,
            "claims": claims,
            "unverified_claims": unverified_claims,
            "confidence": float(data.get("confidence", 0.7)),
        }

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


def _collect_unresolved_claims(tc: dict[str, Any]) -> dict[str, str]:
    unresolved: dict[str, str] = {}
    for key in ("given", "when", "then"):
        text = str(tc.get(key, "") or "")
        if _PLACEHOLDER_RE.search(text):
            unresolved[key] = text
    return unresolved


def _format_unresolved_claims(claims: dict[str, str]) -> str:
    if not claims:
        return "없음"
    return _format_dict_compact(claims, max_chars=1000)
