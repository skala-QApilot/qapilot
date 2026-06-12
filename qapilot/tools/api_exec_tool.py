"""API-mode 검증 실행기 — P1 검증 모드 이원화 (feat/fable/final-performance).

배경 (run 254ca267 전수 해부): 174 TC 중 89% 가 api 필드를 보유하는데 전부
UI 단일 수단으로 검증 → "응답 헤더 X-Trace-Id" 를 assert_visible 로 찾는 류의
수단 오류가 F/U 의 ~22% (+표현력 한계와 합산 시 그 이상). UI 무대가 없는
TC 는 API 직접 호출 + 의도 기반 assert 가 옳은 수단이다.

동작 (SUT 무관 — endpoint/필드/계정 전부 런타임 발견):
1. 인증: auth-negative 의도면 무토큰 (401/403 이 기대), 아니면 test_account 로그인
2. 요청 구성: tc.api 의 path param 은 실존 데이터 (GET 목록 첫 id) 로 해석,
   body 는 TC values 에서 (sensitive placeholder 는 env 해석)
3. 판정: then 에서 기대 상태코드 추출 (명시 4xx 코드 / negative→4xx /
   positive→2xx), 헤더-계약형 then 은 응답 헤더 검사
4. 산출: api_result 호환 call 기록 — 기존 mirror/cross_check 의 의도-인지
   판정이 그대로 verdict 를 만든다 (재사용 극대화)
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from qapilot.shared.logger import get_logger
from qapilot.shared.precondition_fixture import _login, _resolve_ref_ids

logger = get_logger(source="api_exec")

_TIMEOUT = httpx.Timeout(15.0)
_STATUS_CODE_RE = re.compile(r"\b(2\d\d|4\d\d|5\d\d)\b")
_PARAM_RE = re.compile(r"\{([^}/]+)\}")

# then 이 응답 헤더 계약을 검증하는 경우의 헤더 키 추출 (예: "X-Trace-Id 가 포함")
_HEADER_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)\b")


def _expected_statuses(then: str, intent_negative: bool) -> set[int]:
    """then 절에서 기대 상태코드 집합 도출. 명시 코드 우선, 없으면 의도 기반."""
    explicit = {int(m) for m in _STATUS_CODE_RE.findall(then or "")}
    if explicit:
        return explicit
    if intent_negative:
        return set(range(400, 500))
    return set(range(200, 300))


def _header_contract(then: str) -> str | None:
    """then 이 헤더 계약형이면 헤더 키 반환 (예: X-Trace-Id)."""
    t = then or ""
    if not any(k in t.lower() for k in ("헤더", "header")) and "X-Trace" not in t:
        return None
    m = _HEADER_KEY_RE.search(t)
    return m.group(1) if m else None


def _resolve_env_value(value: str) -> str:
    """sensitive placeholder (process.env.TEST_PASSWORD) → 실제 env 값."""
    s = str(value or "")
    if s.startswith("process.env."):
        return os.environ.get(s.split(".", 2)[2], "")
    return s


def _build_body(tc: dict) -> dict[str, Any]:
    """TC values → request body. '{설명}' placeholder/빈 값은 제외."""
    body: dict[str, Any] = {}
    for v in tc.get("values") or []:
        field = str(v.get("field") or "").strip()
        raw = _resolve_env_value(v.get("value"))
        if not field or not raw or (raw.startswith("{") and raw.endswith("}")):
            continue
        body[field] = raw
    return body


async def execute_api_verification(
    *,
    tc: dict,
    base_url: str,
    test_account: dict | None,
    intent_negative: bool,
    auth_negative: bool,
    trace_id: str = "",
) -> dict:
    """단일 TC 의 API-mode 검증. 반환: api_result 호환 dict + verdict.

    {
      "tc_id", "verify_mode": "api",
      "calls": [{method, url, status_code, response_body, ...}],
      "total_calls", "error_calls",
      "verdict": "pass" | "fail",
      "reason": str,
    }
    실패는 전부 graceful — 호출 불능이면 verdict fail + 사유.
    """
    tc_id = str(tc.get("tc_id") or "unknown")
    api = str(tc.get("api") or "").strip()
    then = str(tc.get("then") or "")
    base = (base_url or "").rstrip("/")

    out: dict[str, Any] = {
        "tc_id": tc_id, "verify_mode": "api",
        "calls": [], "total_calls": 0, "error_calls": 0,
        "verdict": "fail", "reason": "",
    }
    if not api or " " not in api or not base:
        out["reason"] = f"api-mode 실행 불가: api={api!r}, base={bool(base)}"
        return out

    method, path = api.split(" ", 1)
    method = method.upper().strip()
    path = path.strip()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        headers: dict[str, str] = {}
        if not auth_negative and test_account:
            token = await _login(client, base, test_account)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                logger.warning("api_exec_login_failed", trace_id=trace_id, tc_id=tc_id)

        # path param 해석 — 실존 데이터 (GET /api/{prefix}s 첫 항목 id)
        params = _PARAM_RE.findall(path)
        if params:
            ref_ids = await _resolve_ref_ids(client, base, headers, params)
            for p in params:
                rid = ref_ids.get(p.lower(), 1)
                path = path.replace("{" + p + "}", str(rid))

        body = _build_body(tc) if method in {"POST", "PUT", "PATCH"} else None
        url = f"{base}{path}"
        try:
            resp = await client.request(method, url, json=body, headers=headers)
        except Exception as e:
            out["reason"] = f"호출 실패: {type(e).__name__}: {e}"
            logger.warning("api_exec_request_failed", trace_id=trace_id, tc_id=tc_id, error=str(e)[:120])
            return out

        try:
            response_body = resp.json() if resp.content else None
        except Exception:
            response_body = None

        try:
            latency_ms = int(resp.elapsed.total_seconds() * 1000)
        except Exception:
            latency_ms = 0
        call = {
            "method": method, "url": url, "status_code": resp.status_code,
            "request_body": body, "response_body": response_body,
            "content_type": resp.headers.get("content-type", ""),
            "latency_ms": latency_ms,
        }
        out["calls"] = [call]
        out["total_calls"] = 1
        out["error_calls"] = 1 if resp.status_code >= 400 else 0

        # 판정 ① 헤더 계약형
        header_key = _header_contract(then)
        if header_key:
            present = header_key in resp.headers
            negative_presence = any(t in then for t in ("않는다", "않습니다", "없어야"))
            ok = (not present) if negative_presence else present
            out["verdict"] = "pass" if ok else "fail"
            out["reason"] = (
                f"헤더 {header_key} {'부재' if negative_presence else '존재'} 기대 — "
                f"실제 {'존재' if present else '부재'}"
            )
            return out

        # 판정 ② 상태코드 계약
        expected = _expected_statuses(then, intent_negative)
        ok = resp.status_code in expected
        # 5xx 는 어떤 의도에서도 결함 신호
        if resp.status_code >= 500 and 500 not in expected and resp.status_code not in expected:
            ok = False
        out["verdict"] = "pass" if ok else "fail"
        exp_repr = (
            sorted(expected)[:4] if len(expected) <= 4
            else f"{min(expected)}~{max(expected)}"
        )
        out["reason"] = f"기대 status {exp_repr} — 실제 {resp.status_code}"
        return out
