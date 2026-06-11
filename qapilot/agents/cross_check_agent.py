"""Cross-check Agent.

UI 실행 결과와 API 응답, DB 상태를 비교하여
데이터 정합성 불일치를 자동 탐지한다.

담당: E
Created: 2026-05-07
"""

import json
import re
from typing import Any

from qapilot.agents.base_agent import BaseAgent
from qapilot.shared.schemas import (
    CrossCheckMismatch,
    CrossCheckResult,
    ExecuteResult,
)

# 이슈 #131 — CrossCheck LLM input 압축 정책. 이전 e2e (trace `db24608c`) 에서 285K
# tokens (gpt-4o-mini 128K 한계의 2.2배) context_length_exceeded 가 모든 호출 차단한
# 사례 해결.
#
# 의미 보존 원칙 (spec §4.5.1 + prompts/cross_check/system.md 의 요구사항 기반):
# 정합성 분석의 핵심 정보 (ui_value / api_value / db_value / 필드명 / status_code / url /
# error_code prefix) 는 100% 보존하고, 분석에 불필요한 거대 페이로드만 정제.
#
# 정확도 위협 차단:
# - API response_body: JSON 인식 시 핵심 필드 (`status`, `error`, `message`, `code`,
#   `id`, `amount`, `name`, `success`, ...) 우선 추출. parse 실패 시만 char cap.
# - UI error: TOOL_UI_ASSERTION_FAIL 패턴의 `expected ... got ...` 우선 추출.
# - DB rows: 정합성 분석 row 그대로 (10개 cap).
_MAX_BODY_CHARS = 1500           # JSON parse 실패 시 fallback char cap (500 → 1500 확장)
_MAX_ERROR_CHARS = 1000          # UI step.error 의 raw text cap (assertion 패턴 추출 안 될 때)
_MAX_CONSOLE_LOG_CHARS = 500     # console_log 라인 1개당 cap (작은 값 — 노이즈 위주)
_MAX_LLM_INPUT_CHARS = 120_000   # 전체 LLM 입력 dump 최대 (~30K tokens, 128K 의 ~25%)
_MAX_API_CALLS = 30              # api_trace.calls 최대 보존 (이상은 슬라이스)
_MAX_DB_SNAPSHOTS = 20           # db_result.snapshots 최대 보존
_MAX_DB_ROWS_PER_SNAPSHOT = 10   # snapshot 의 rows 최대 보존

# API response_body 의 JSON 파싱 시 우선 추출할 의미적 핵심 필드 (정합성 분석 본질).
# 명시적 화이트리스트로 보존 — 일반 페이로드의 큰 부피 차단하면서 정합성 비교 값 살림.
_API_RESPONSE_KEY_FIELDS = {
    "status", "error", "message", "code", "id", "amount", "name", "success",
    "result", "data", "errors", "error_code", "detail", "title",
    # 도메인 특화 (BSS 등)
    "plan_id", "order_id", "user_id", "customer_id", "contract_id",
    "phone", "email",
}

# UI error 메시지의 정합성 분석 핵심 패턴 — assert 단언 실패의 expected vs got.
# 예: "TOOL_UI_ASSERTION_FAIL: expected '가입 완료' got '오류 발생'"
_ASSERTION_FAIL_PATTERN = re.compile(
    r"(?:expected|기대(?:값)?)\s*[:=]?\s*['\"]([^'\"]{0,200})['\"][\s\S]{0,200}?"
    r"(?:got|실제|received|actual)\s*[:=]?\s*['\"]([^'\"]{0,200})['\"]",
    re.IGNORECASE,
)


class CrossCheckAgent(BaseAgent):
    """Cross-check Agent.

    역할: UI↔API↔DB 데이터 정합성 검증, 정합성 점수 산출
    입력: UITestResult, APITraceResult, DBTestResult
    출력: CrossCheckResult, confidence
    호출 Tool: 없음
    """

    agent_name = "cross_check"

    def _extract_error_code(
    self, ui_result: dict, api_trace: dict, db_result: dict
    ) -> str | None:
        """UI/API/DB 응답에서 에러 코드 추출."""
        # UI 에러 코드 추출 (TOOL_UI_ 접두사)
        steps = ui_result.get("steps", [])
        for step in steps:
            if step.get("status") == "fail":
                error = step.get("error", "") or ""
                if error.startswith("TOOL_UI_"):
                    return error.split(":")[0].strip()

        # API 에러 코드 추출
        calls = api_trace.get("calls", [])
        for call in calls:
            status_code = call.get("status_code", 200)
            if status_code >= 400:
                response_body = call.get("response_body", {}) or {}
                if isinstance(response_body, dict) and "code" in response_body:
                    return response_body["code"]
                return str(status_code)

        # DB 에러 코드 추출 (HTTP 상태 코드)
        db_error = db_result.get("error_code")
        if db_error:
            return str(db_error)

        return None

    async def _analyze_with_llm(
        self,
        ui_result: dict,
        api_trace: dict,
        db_result: dict,
        last_error: str | None,
    ) -> tuple[list[CrossCheckMismatch], float, int, str, str]:
        """LLM으로 UI/API/DB 불일치 분석.

        이슈 #131: LLM 호출 직전 input 압축 — context_length_exceeded 차단.
        정합성 분석에 필요한 핵심 필드는 보존하되 거대 페이로드 (body, console_log,
        screenshot path, headers 등) 정제. 285K → ~5-15K tokens 수준.
        """
        # 이슈 #131 — input 압축 적용
        compact_ui, compact_api, compact_db = self._compact_for_llm(
            ui_result, api_trace, db_result
        )
        context_json = json.dumps(
            {
                "ui_result": compact_ui,
                "api_trace": compact_api,
                "db_result": compact_db,
            },
            ensure_ascii=False,
            indent=2,
        )
        # 압축 후에도 _MAX_LLM_INPUT_CHARS 초과 시 hard cut (안전망)
        if len(context_json) > _MAX_LLM_INPUT_CHARS:
            self.logger.warning(
                "cross_check_context_hard_truncated",
                original_chars=len(context_json),
                truncated_to=_MAX_LLM_INPUT_CHARS,
            )
            context_json = context_json[:_MAX_LLM_INPUT_CHARS] + '\n  /* ... truncated ... */'

        system_prompt = self.prompts.system()
        user_prompt = self.prompts.render(
            context=context_json,
            input_data=json.dumps(
                {
                    "task": "UI, API, DB 데이터를 비교하여 불일치를 탐지하고 JSON으로 반환하세요.",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

        user_prompt = self.with_correction_hint(user_prompt, last_error)
        response = await self.llm.chat(system_prompt, user_prompt)

        try:
            parsed = json.loads(response.content)
            mismatches = [CrossCheckMismatch(**m) for m in parsed.get("mismatches", [])]
            match_score = float(parsed.get("match_score", 1.0))
            matched_fields = int(parsed.get("matched_fields", 0))
            error_code = parsed.get("error_code", "none")
            summary = parsed.get("summary", "")
        except Exception:
            # 분석 실패를 조용한 pass (score 1.0) 로 둔갑시키지 않는다 —
            # CC_PARSE_FAIL 로 표시해 pipeline 이 unverified 처리.
            mismatches = []
            match_score = 0.0
            matched_fields = 0
            error_code = "CC_PARSE_FAIL"
            summary = "cross_check LLM 응답 파싱 실패 — 정합성 분석 미수행 (unverified)"

        return mismatches, match_score, matched_fields, error_code, summary

    def _evaluate_scenario_intent(
        self,
        scenario_intent: dict[str, Any],
        ui_result: dict,
        api_trace: dict,
    ) -> bool:
        """#259: 시나리오 의도 (then/tags) 도달 여부 heuristic 판정.

        Returns True 시 has_mismatch=False (pass) 처리. e2e trace `2cf12739` 본질.

        분류:
        - positive (tags=[normal] 또는 then 에 "성공"/"완료") + actual success → True
        - negative error (tags=[edge_case] + then 에 4xx/오류/실패/거부) + actual 4xx → True
        - negative validation (then 에 "빈"/"필수"/"입력"/"형식") + actual POST 0 또는 4xx → True
        - 그 외 → False (기존 mismatch 로직 따름)
        """
        if not scenario_intent:
            return False
        then_text = (scenario_intent.get("then") or "").lower()
        tags = [str(t).lower() for t in (scenario_intent.get("tags") or [])]
        name = (scenario_intent.get("name") or "").lower()

        is_negative = (
            "edge_case" in tags or "negative" in tags or "error" in tags
            or any(k in then_text for k in [
                "4xx", "5xx", "400", "401", "403", "404", "409", "422", "500",
                "오류", "실패", "에러", "거부", "불가", "차단", "잘못", "invalid", "error",
                "conflict", "bad request",
            ])
            or any(k in name for k in ["오류", "실패", "edge"])
        )
        is_validation_negative = is_negative and any(k in then_text for k in [
            "빈", "필수", "입력", "형식", "validation", "required",
        ])

        # actual outcome — API 4xx 응답 또는 UI form-level prevent
        calls = api_trace.get("calls") or []
        api_4xx_or_5xx = any(
            (c.get("status_code") or 0) >= 400 for c in calls
        )
        # UI form prevent — submit 후 페이지 변화 0 + POST 0 (validation 막힘)
        post_count = sum(
            1 for c in calls
            if (c.get("method") or "").upper() == "POST"
        )
        ui_steps = ui_result.get("steps") or []
        ui_click_pass = any(
            (s.get("action") in {"click", "click_submit"}) and s.get("status") == "pass"
            for s in ui_steps
        )
        ui_form_prevent = ui_click_pass and post_count == 0

        # 입력 완전성 가드 (run d20fc18f TS-001-TC-03 실증): 매핑 결손으로 의도한
        # 입력 (예: 7자 비밀번호) 을 만들지 못한 채 form prevent 가 일어난 경우,
        # "검증 통과" 구제는 잘못된 인과 — 의도한 규칙은 전혀 검증되지 않았다.
        inputs_complete = scenario_intent.get("inputs_complete")
        if is_validation_negative:
            if inputs_complete is False:
                return False
            return ui_form_prevent or api_4xx_or_5xx
        if is_negative:
            return api_4xx_or_5xx
        # positive — actual success. POST 2xx 는 TC 의 대상 endpoint 와 일치할
        # 때만 증거로 인정 (run d20fc18f TS-006-TC-05 실증: 자동 로그인 POST 200
        # 이 '가입 완료' 증거로 둔갑하던 격차).
        expected_path = self._expected_api_path(scenario_intent.get("api"))
        api_2xx_post = any(
            (c.get("method") or "").upper() in {"POST", "PUT", "PATCH"}
            and 200 <= (c.get("status_code") or 0) < 300
            and (not expected_path or expected_path in str(c.get("url") or ""))
            for c in calls
        )
        ui_all_pass = all(s.get("status") == "pass" for s in ui_steps) if ui_steps else False
        return api_2xx_post or ui_all_pass

    @staticmethod
    def _expected_api_path(api: Any) -> str:
        """TC.api ("POST /api/orders/{id}/cancel") → URL 대조용 고정 prefix
        ("/api/orders/"). path param 이전까지만 — 없으면 빈 문자열 (대조 생략)."""
        s = str(api or "").strip()
        if " " in s:
            s = s.split(" ", 1)[1]
        if not s.startswith("/"):
            return ""
        return s.split("{", 1)[0]

    async def _execute(
        self,
        context: dict[str, Any],
        params: dict[str, Any],
        last_error: str | None = None,
    ) -> ExecuteResult:
        """경로 A/B 분기 후 Cross-check 수행."""
        tc_id = params.get("tc_id", "unknown")
        ui_result = context.get("ui_result", {})
        api_trace = context.get("api_trace", {})
        db_result = context.get("db_result", {})
        # #259 본질 fix — 시나리오 의도 (then/tags) 기반 pass/fail 정확 판정.
        # negative TC (tags=[edge_case] 또는 then 에 4xx/오류 키워드) 의 outcome
        # 도달 (API 4xx 응답 또는 UI form prevent) 시 intent_satisfied=True.
        # → has_mismatch=False (pass) 판정. e2e trace `2cf12739` 본질.
        scenario_intent = context.get("scenario_intent") or {}
        intent_satisfied = self._evaluate_scenario_intent(
            scenario_intent, ui_result, api_trace
        )

        # 경로 A: 에러 코드 있는 경우
        error_code = self._extract_error_code(ui_result, api_trace, db_result)
        if error_code:
            has_mismatch = not intent_satisfied
            result = CrossCheckResult(
                tc_id=tc_id,
                match_score=1.0 if intent_satisfied else 0.0,
                matched_fields=1 if intent_satisfied else 0,
                mismatched_fields=0,
                mismatches=[],
                has_mismatch=has_mismatch,
            )
            # 경로 A summary는 LLM으로 생성
            _, _, _, _, summary = await self._analyze_with_llm(
                ui_result, api_trace, db_result, last_error
            )
            return ExecuteResult(
                result={
                    "cross_check": result,
                    "error_code": error_code,
                    "intent_satisfied": intent_satisfied,
                    "summary": summary,
                    "route": "A",
                },
                confidence=1.0,
            )

        # 경로 B: 에러 코드 없는 경우 → LLM으로 불일치 분석
        mismatches, match_score, matched_fields, mismatch_code, summary = await self._analyze_with_llm(
            ui_result, api_trace, db_result, last_error
        )

        # #259 본질 — Path B 에서도 intent_satisfied 가 mismatch 판정 우선
        if intent_satisfied:
            has_mismatch = False
        else:
            has_mismatch = len(mismatches) > 0

        result = CrossCheckResult(
            tc_id=tc_id,
            match_score=match_score if not intent_satisfied else max(match_score, 1.0),
            matched_fields=matched_fields,
            mismatched_fields=len(mismatches),
            mismatches=mismatches,
            has_mismatch=has_mismatch,
        )

        return ExecuteResult(
            result={
                "cross_check": result,
                "error_code": mismatch_code,
                "summary": summary,
                "route": "B",
            },
            confidence=match_score,
        )

    # ── 이슈 #131: CrossCheck LLM input 압축 ────────────────────────────────

    def _compact_for_llm(
        self, ui_result: dict, api_trace: dict, db_result: dict
    ) -> tuple[dict, dict, dict]:
        """LLM 호출 input 압축 (이슈 #131).

        정합성 분석 (UI↔API↔DB) 의 의미를 100% 보존하면서 거대 페이로드 정제:
        - ui_result.steps: screenshot_path 제거 + error 는 assertion-pattern 우선 추출
          (실패 시 _MAX_ERROR_CHARS cap) + console_logs 마지막 5개
        - api_trace.calls: body 는 JSON parse 시 핵심 필드 (status/error/amount 등)
          우선 추출, 실패 시 _MAX_BODY_CHARS cap. headers 제거. 30 calls cap (실패 우선)
        - db_result.snapshots: 20개 cap + rows 10개 cap

        Returns: (compact_ui, compact_api, compact_db) — 원본 변경 없음.
        """
        return (
            self._compact_ui_result(ui_result),
            self._compact_api_trace(api_trace),
            self._compact_db_result(db_result),
        )

    def _compact_ui_result(self, ui_result: dict) -> dict:
        """ui_result 압축 — screenshot_path 제거.

        UI error 는 정합성 분석 핵심 (`expected vs got` 의 ui_value/기대값 보유).
        assertion 패턴이 감지되면 그 부분을 우선 보존 + 나머지는 작은 head/tail로.
        패턴이 없으면 첫 _MAX_ERROR_CHARS 자.
        """
        if not isinstance(ui_result, dict):
            return ui_result if ui_result is not None else {}
        out = {k: v for k, v in ui_result.items() if k != "steps"}
        compact_steps: list[dict] = []
        for step in (ui_result.get("steps") or []):
            if not isinstance(step, dict):
                continue
            s = {
                k: v for k, v in step.items()
                if k not in ("screenshot_path",)
            }
            s["error"] = _compact_ui_error(step.get("error"))
            logs = step.get("console_logs") or []
            if isinstance(logs, list):
                s["console_logs"] = [
                    _truncate_str(str(x), _MAX_CONSOLE_LOG_CHARS) for x in logs[-5:]
                ]
            compact_steps.append(s)
        out["steps"] = compact_steps
        return out

    def _compact_api_trace(self, api_trace: dict) -> dict:
        """api_trace 압축 — calls 의 body JSON-aware 압축 + headers 제거.

        - request/response body 가 JSON 으로 parse 되면 핵심 필드 (_API_RESPONSE_KEY_FIELDS)
          우선 추출. 정합성 분석의 api_value (status / amount / error 등) 100% 보존.
        - parse 실패 시 _MAX_BODY_CHARS char cap (1500자).
        - calls 30개 cap (실패 우선).
        """
        if not isinstance(api_trace, dict):
            return api_trace if api_trace is not None else {}
        calls = api_trace.get("calls") or []
        compact_calls: list[dict] = []
        sliced = self._slice_api_calls(calls, _MAX_API_CALLS)
        for call in sliced:
            if not isinstance(call, dict):
                continue
            c = {
                k: v for k, v in call.items()
                if k not in ("request_headers", "response_headers")
            }
            c["request_body"] = _compact_api_body(call.get("request_body"))
            c["response_body"] = _compact_api_body(call.get("response_body"))
            compact_calls.append(c)
        out = {k: v for k, v in api_trace.items() if k != "calls"}
        out["calls"] = compact_calls
        if len(calls) > len(compact_calls):
            out["_truncated_calls"] = len(calls) - len(compact_calls)
        return out

    def _compact_db_result(self, db_result: dict) -> dict:
        """db_result 압축 — snapshots 최대 _MAX_DB_SNAPSHOTS, rows 최대 _MAX_DB_ROWS_PER_SNAPSHOT."""
        if not isinstance(db_result, dict):
            return db_result if db_result is not None else {}
        snapshots = db_result.get("snapshots") or []
        compact_snapshots: list[dict] = []
        for snap in snapshots[:_MAX_DB_SNAPSHOTS]:
            if not isinstance(snap, dict):
                continue
            s = dict(snap)
            rows = s.get("rows") or s.get("data") or []
            if isinstance(rows, list) and len(rows) > _MAX_DB_ROWS_PER_SNAPSHOT:
                s["rows"] = rows[:_MAX_DB_ROWS_PER_SNAPSHOT]
                s["_truncated_rows"] = len(rows) - _MAX_DB_ROWS_PER_SNAPSHOT
            compact_snapshots.append(s)
        out = {k: v for k, v in db_result.items() if k != "snapshots"}
        out["snapshots"] = compact_snapshots
        if len(snapshots) > len(compact_snapshots):
            out["_truncated_snapshots"] = len(snapshots) - len(compact_snapshots)
        return out

    @staticmethod
    def _slice_api_calls(calls: list, max_calls: int) -> list:
        """API call 슬라이스 — 실패 (status>=400) 우선 + 나머지 first N/2 + last N/2."""
        if len(calls) <= max_calls:
            return list(calls)
        failed = [c for c in calls if isinstance(c, dict) and int(c.get("status_code", 0) or 0) >= 400]
        rest = [c for c in calls if c not in failed]
        if len(failed) >= max_calls:
            return failed[:max_calls]
        remaining = max_calls - len(failed)
        half = remaining // 2
        return failed + rest[:half] + rest[-(remaining - half):]


def _truncate_str(value: Any, max_chars: int) -> str | None:
    """str cap. None 은 None 유지."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"... [{len(s) - max_chars} more chars]"


def _stringify(value: Any) -> str | None:
    """dict/list → JSON str. None / str → as-is."""
    if value is None or isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _compact_api_body(value: Any) -> str | None:
    """API body 압축 (JSON-aware) — 이슈 #131.

    정합성 분석의 본질은 body 내 핵심 필드 (`status`, `amount`, `error` 등) 값.
    단순 char cap 은 그 필드가 뒤에 있으면 누락시킴 → JSON parse 후 화이트리스트
    필드 우선 추출하여 의미를 100% 보존.

    동작:
    - None → None
    - str + JSON parse 성공 → 핵심 필드 추출. 결과가 _MAX_BODY_CHARS 이하면 그대로.
      초과 시 핵심 필드 결과만 (그 안에서도 큰 값은 _MAX_BODY_CHARS//2 로 cap).
    - str + JSON parse 실패 → 첫 _MAX_BODY_CHARS 자
    - dict / list → JSON parse 성공 케이스와 동일
    """
    if value is None:
        return None

    # JSON 파싱 시도
    parsed: Any = None
    if isinstance(value, (dict, list)):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return _truncate_str(value, _MAX_BODY_CHARS)
    else:
        return _truncate_str(str(value), _MAX_BODY_CHARS)

    # parsed 가 dict 이면 핵심 필드 추출 후 dump
    full_json = json.dumps(parsed, ensure_ascii=False)
    if len(full_json) <= _MAX_BODY_CHARS:
        return full_json

    if isinstance(parsed, dict):
        extracted = _extract_key_fields(parsed)
        compact_json = json.dumps(extracted, ensure_ascii=False)
        # 그래도 너무 크면 char cap
        if len(compact_json) > _MAX_BODY_CHARS:
            return _truncate_str(compact_json, _MAX_BODY_CHARS)
        # 잘려나간 필드 정보 표시
        dropped = [k for k in parsed.keys() if k not in extracted]
        if dropped:
            extracted["_dropped_keys"] = dropped[:20]
            compact_json = json.dumps(extracted, ensure_ascii=False)
        return _truncate_str(compact_json, _MAX_BODY_CHARS)

    # list 등은 char cap
    return _truncate_str(full_json, _MAX_BODY_CHARS)


def _extract_key_fields(obj: dict, depth: int = 0) -> dict:
    """dict 에서 _API_RESPONSE_KEY_FIELDS 우선 추출 (재귀, 깊이 제한 2)."""
    if depth > 2:
        # 깊이 초과 시 keys 만
        return {"_keys": list(obj.keys())[:20]}
    out: dict = {}
    for k, v in obj.items():
        if k in _API_RESPONSE_KEY_FIELDS:
            if isinstance(v, dict):
                out[k] = _extract_key_fields(v, depth + 1)
            elif isinstance(v, list):
                # list 안의 dict 도 재귀
                if v and isinstance(v[0], dict):
                    out[k] = [_extract_key_fields(item, depth + 1) for item in v[:5]]
                    if len(v) > 5:
                        out[k].append({"_truncated_items": len(v) - 5})
                else:
                    out[k] = v[:10] if isinstance(v, list) else v
            else:
                # primitive — char cap (한 필드 값이 너무 클 경우)
                if isinstance(v, str) and len(v) > _MAX_BODY_CHARS // 2:
                    out[k] = _truncate_str(v, _MAX_BODY_CHARS // 2)
                else:
                    out[k] = v
    return out


def _compact_ui_error(value: Any) -> str | None:
    """UI step.error 압축 — 이슈 #131.

    assertion 패턴 (expected ... got ...) 이 감지되면 그 부분 우선 보존.
    패턴 없으면 prefix (TOOL_UI_*) + 첫 _MAX_ERROR_CHARS 자.
    """
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    if len(s) <= _MAX_ERROR_CHARS:
        return s

    # TOOL_UI_ASSERTION_FAIL: expected 'X' got 'Y' 패턴 추출
    m = _ASSERTION_FAIL_PATTERN.search(s)
    if m:
        # prefix (첫 줄의 error code) + assertion 패턴 그대로 보존
        first_line = s.split("\n", 1)[0][:200]
        return f"{first_line}\n[assertion] expected={m.group(1)!r} got={m.group(2)!r}"

    return _truncate_str(s, _MAX_ERROR_CHARS)