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
from qapilot.shared.precondition_fixture import (
    _apply_field_error,
    _field_errors_from_422,
    _login,
    _resolve_ref_ids,
)

logger = get_logger(source="api_exec")

_TIMEOUT = httpx.Timeout(15.0)
_STATUS_CODE_RE = re.compile(r"\b(2\d\d|4\d\d|5\d\d)\b")
_PARAM_RE = re.compile(r"\{([^}/]+)\}")

# then 이 응답 헤더 계약을 검증하는 경우의 헤더 키 추출 (예: "X-Trace-Id 가 포함")
_HEADER_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)\b")

# ── P1.5 DB 관찰 축 ──────────────────────────────────────────────────────
# then 이 DB 상태로만 검증 가능한 계약인 경우 (API 응답에 해시가 노출되지
# 않는 게 정상이므로 응답 검사로는 불가). faults 의 expected_observation.db_state
# 류 (FR-009 결함 검출) 도 이 축이 담당한다.
_DB_HASH_TOKENS = ("해시", "bcrypt", "hash")
_DB_PLACE_TOKENS = ("db에", "데이터베이스", "테이블")


def _db_contract(then: str) -> str | None:
    """then 이 DB-계약형이면 검사 종류 반환 — "hash" | "exists" | None.

    "저장된다" 단독은 UI 로도 확인 가능한 표현이라 트리거하지 않는다 —
    해시류 또는 명시적 DB 어휘 (db에/데이터베이스/테이블) 가 있어야 한다.
    """
    t = (then or "").lower()
    if any(k in t for k in _DB_HASH_TOKENS) and any(k in t for k in ("저장", "암호화", "기록")):
        return "hash"
    # "테이블" 은 HTML table 표시 표현과 겹친다 ("목록이 테이블에 표시된다")
    # — 쓰기 동사가 함께 있어야 DB-계약으로 인정.
    if any(k in t for k in _DB_PLACE_TOKENS) and any(
        k in t for k in ("저장", "기록", "생성", "추가", "삽입", "남는다")
    ):
        return "exists"
    return None


def _row_matches(row: dict, body: dict | None) -> bool:
    """Act 요청 body 의 값 (식별자) 과 일치하는 row 인지. password 류는
    DB 에 평문이 없는 게 정상이라 식별자 자격이 없다."""
    for k, v in (body or {}).items():
        if "password" in k.lower():
            continue
        rv = row.get(k)
        if rv is not None and str(rv) == str(v):
            return True
    return False


def _observe_db(kind: str, snapshot: dict | None, body: dict | None) -> tuple[bool, str]:
    """DB 스냅샷 관찰 — (ok, 사유). 스냅샷 부재는 검증 실패 (false-pass 금지)."""
    rows = [r for r in ((snapshot or {}).get("rows") or []) if isinstance(r, dict)]
    if not rows:
        return False, "DB 관찰 실패: 스냅샷 빈 상태/조회 불가"

    if kind == "hash":
        # Act 가 409 (이미 존재) 여도 기존 row 의 저장 형식 관찰은 유효한
        # 계약 검증이다 — 식별자 일치 row 우선, 없으면 전체 row.
        targets = [r for r in rows if _row_matches(r, body)] or rows
        cols = [c for c in targets[0] if "hash" in c.lower() or "password" in c.lower()]
        if not cols:
            return False, "DB 관찰 실패: hash/password 컬럼 부재"
        col = cols[0]
        vals = [r.get(col) for r in targets if r.get(col)]
        if not vals:
            return False, f"DB 관찰 실패: {col} 값 전부 빈 상태"
        bad = [v for v in vals if not str(v).startswith("$2")]
        if bad:
            return False, f"DB 관찰: {col} {len(bad)}/{len(vals)}건이 비-bcrypt (평문 의심)"
        return True, f"DB 관찰: {col} {len(vals)}건 전부 bcrypt($2) 형식"

    # exists — Act 에서 쓴 식별자 값과 일치하는 row 존재 확인
    if not body:
        return False, "DB 관찰 실패: 대조할 요청 식별자 없음 (body 없는 요청)"
    matched = [r for r in rows if _row_matches(r, body)]
    if matched:
        return True, f"DB 관찰: 요청 값 일치 row {len(matched)}건 존재"
    return False, "DB 관찰: 요청 값 일치 row 미발견"


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


def _uniquify_email_in_body(body: dict, path: str, intent_negative: bool, trace_id: str) -> None:
    """positive 의도 signup body 의 email 에 run suffix — run 간 데이터 격리.

    UI 경로의 _uniquify_signup_email_for_run 과 동형 (api-mode body 판).
    negative (중복 의도) 는 기존 값 보존."""
    if intent_negative or "signup" not in (path or "").lower():
        return
    suffix = (trace_id or "").replace("-", "")[:8]
    if not suffix:
        return
    for k, v in list(body.items()):
        s = str(v or "")
        if "email" in k.lower() and "@" in s and f"+{suffix}@" not in s:
            local, _, domain = s.partition("@")
            body[k] = f"{local}+{suffix}@{domain}"


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


# ── P3 observe 스펙 인터프리터 ────────────────────────────────────────────
# then(산문) 의 휴리스틱 해석을 대체하는 기계 실행 명세. TC 스키마에
# observe: [{kind, ...}] 가 있으면 verdict = 전 observe 의 AND — cc 의
# LLM 재추론/키워드 휴리스틱이 만들던 오분류 클래스 (부재-긍정 등) 를
# 원천 제거한다. 미보유 TC 는 기존 휴리스틱 폴백 (하위호환).
_API_OBSERVE_KINDS = ("http_status", "http_header", "response_body", "db_field")
_JSONPATH_TOKEN_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+|\*)\]")


def _jsonpath_lite(data: Any, path: str) -> list[Any]:
    """최소 jsonpath — $.a.b / $[0].x / $[*].is_current 지원."""
    vals: list[Any] = [data]
    for name, idx in _JSONPATH_TOKEN_RE.findall(path or ""):
        nxt: list[Any] = []
        for v in vals:
            if name:
                if isinstance(v, dict) and name in v:
                    nxt.append(v[name])
            elif idx == "*":
                if isinstance(v, list):
                    nxt.extend(v)
            else:
                i = int(idx)
                if isinstance(v, list) and i < len(v):
                    nxt.append(v[i])
        vals = nxt
    return vals


def _is_tautological_predicate(pred: dict | None) -> bool:
    """항진 predicate — 어떤 값이든 통과해 무검증 observe 가 되는 경우.

    run 84c0e1eb 생성 검수 실증: then 'is_current=true' 인데 LLM 이
    {"in": [true, false]} 를 생성 — 약한 pass 재발 경로라 폐기 대상."""
    if not isinstance(pred, dict):
        return False
    in_vals = {str(x).lower() for x in (pred.get("in") or [])}
    return {"true", "false"} <= in_vals


def _eval_predicate(values: list[Any], pred: dict | None) -> bool:
    """값 목록에 대한 predicate — 하나라도 충족하면 True (any-match)."""
    if not values:
        return False
    if not pred:
        return True  # 존재 자체가 조건
    for v in values:
        s = str(v)
        if "eq" in pred and s == str(pred["eq"]):
            return True
        if "ne" in pred and s != str(pred["ne"]):
            return True
        if "matches" in pred:
            try:
                if re.search(str(pred["matches"]), s):
                    return True
            except re.error:
                return False
        if "in" in pred and any(s == str(x) for x in (pred["in"] or [])):
            return True
        if pred.get("nonempty") and s.strip() not in ("", "None", "null"):
            return True
        if pred.get("exists"):
            return True
    return False


def _resolve_where_template(value: Any, request_body: dict | None) -> Any:
    """where 값의 {request.field} 템플릿 → 실제 요청 값."""
    s = str(value or "")
    if s.startswith("{request.") and s.endswith("}"):
        return (request_body or {}).get(s[9:-1])
    return value


async def _eval_observe(
    obs: dict, resp: Any, response_body: Any,
    request_body: dict | None, snapshot_fetch, db_table_hint: str | None,
) -> tuple[bool, str]:
    """observe 1건 평가 → (ok, 사유). 평가 불능은 fail (false-pass 금지)."""
    kind = str(obs.get("kind") or "")
    absent = bool(obs.get("absent"))

    if kind == "http_status":
        expected = [int(x) for x in (obs.get("expected") or []) if str(x).isdigit()]
        ok = resp.status_code in expected if expected else False
        return ok, f"status {resp.status_code} (기대 {expected})"

    if kind == "http_header":
        name = str(obs.get("name") or "")
        present = name in resp.headers
        ok = (not present) if absent else present
        return ok, f"헤더 {name} {'부재' if absent else '존재'} 기대 — 실제 {'존재' if present else '부재'}"

    if kind == "response_body":
        values = _jsonpath_lite(response_body, str(obs.get("path") or ""))
        if absent:
            ok = not _eval_predicate(values, obs.get("predicate"))
            return ok, f"body {obs.get('path')} 부재 기대 — 값 {len(values)}건"
        ok = _eval_predicate(values, obs.get("predicate"))
        return ok, f"body {obs.get('path')} → {[str(v)[:20] for v in values[:3]]} predicate={'충족' if ok else '미충족'}"

    if kind == "db_field":
        table = str(obs.get("table") or db_table_hint or "")
        if not (snapshot_fetch and table):
            return False, f"db_field 관찰 불가 (table={table!r}, fetcher={bool(snapshot_fetch)})"
        try:
            snapshot = await snapshot_fetch(table)
        except Exception as e:
            return False, f"db_field 스냅샷 실패: {type(e).__name__}"
        rows = [r for r in ((snapshot or {}).get("rows") or []) if isinstance(r, dict)]
        where = {
            k: _resolve_where_template(v, request_body)
            for k, v in (obs.get("where") or {}).items()
        }
        matched = [
            r for r in rows
            if all(str(r.get(k)) == str(v) for k, v in where.items())
        ] if where else rows
        if absent:
            ok = not matched
            return ok, f"db {table} row 부재 기대 — 일치 {len(matched)}건"
        if not matched:
            return False, f"db {table} 일치 row 없음 (where={where})"
        field = str(obs.get("field") or "")
        if not field:
            return True, f"db {table} row {len(matched)}건 존재"
        values = [r.get(field) for r in matched]
        ok = _eval_predicate(values, obs.get("predicate"))
        return ok, f"db {table}.{field} → {[str(v)[:20] for v in values[:3]]} predicate={'충족' if ok else '미충족'}"

    return False, f"미지원 observe kind: {kind}"


# ── P2 상태-인지 값 접지 + 전제 상태 조성 ──────────────────────────────────
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DESTRUCTIVE_SEGMENTS = ("cancel", "terminate", "leave", "deactivate")
_CONFLICT_HINTS = ("이미", "중복", "duplicate", "conflict")


def _bump_past_dates(body: dict, intent_negative: bool) -> list[str]:
    """positive 의도 body 의 과거 날짜 값 → 미래 (+7일).

    TV 는 시나리오 생성 시점의 날짜를 정적으로 저장 — run 시점엔 과거가
    되어 예약/예정 류 API 가 400 (run 2464603c: TS-008 effective_date
    2023-10-06). 과거 날짜 거부 검증은 negative 의도라 게이트로 보존."""
    if intent_negative:
        return []
    import datetime as _dt
    future = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
    bumped = []
    for k, v in list(body.items()):
        s = str(v or "")
        if _DATE_VALUE_RE.match(s) and s < _dt.date.today().isoformat():
            body[k] = future
            bumped.append(k)
    return bumped


def _rows_from_response(data: Any) -> list[dict]:
    """GET 응답 → row 리스트 (list 직접 또는 dict 안의 첫 list 값)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


async def _candidate_values(
    client: httpx.AsyncClient, base: str, headers: dict,
    field: str, current: Any, url_path: str,
) -> list[Any]:
    """body 필드의 대안 값 후보 — SUT 현재 상태 (관련 컬렉션 GET) 에서 조회.

    - plan_id 류: /api/{stem}s 컬렉션의 다른 id
    - label 류: 요청 경로 복수형 (payment-method → payment-methods) 의 동명 필드
    """
    sources: list[str] = []
    if field.endswith("_id"):
        sources.append(f"{base}/api/{field[:-3]}s")
    sources.append(f"{base}{url_path}s")
    out: list[Any] = []
    for src in sources:
        try:
            r = await client.get(src, headers=headers)
        except Exception:
            continue
        if r.status_code >= 300:
            continue
        try:
            rows = _rows_from_response(r.json())
        except Exception:
            continue
        key = "id" if field.endswith("_id") else field
        for row in rows:
            v = row.get(key)
            if v is not None and str(v) != str(current) and v not in out:
                out.append(v)
        if out:
            break
    return out[:3]


async def _create_dedicated_resource(
    client: httpx.AsyncClient, base: str, headers: dict, param: str,
) -> Any | None:
    """파괴적 TC 전용 리소스 생성 — 공유 데이터 (목록 첫 id) 를 해지/삭제해
    후속 TC 를 오염시키던 상태 간섭 차단 (run 04d5f79e: TS-009 의 cancel 이
    order 1 을 실제 해지 → TS-011 toggle 400 연쇄). 422 사다리로 body 자가구성."""
    stem = param.lower().removesuffix("_id")
    url = f"{base}/api/{stem}s"
    body: dict[str, Any] = {}
    for _ in range(4):
        try:
            r = await client.post(url, json=body, headers=headers)
        except Exception:
            return None
        if r.status_code < 300:
            try:
                created = r.json()
            except Exception:
                return None
            return (created or {}).get("id") if isinstance(created, dict) else None
        if r.status_code != 422:
            return None
        try:
            errors = _field_errors_from_422(r.json())
        except Exception:
            return None
        if not errors:
            return None
        ref_ids = await _resolve_ref_ids(
            client, base, headers,
            [str(loc[-1]) for loc, _, _ in errors if not isinstance(loc[-1], int)],
        )
        for loc_path, err_type, ctx in errors:
            _apply_field_error(body, loc_path, err_type, ctx, ref_ids)
    return None


async def execute_api_verification(
    *,
    tc: dict,
    base_url: str,
    test_account: dict | None,
    intent_negative: bool,
    auth_negative: bool,
    trace_id: str = "",
    db_table: str | None = None,
    snapshot_fetch=None,
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

        # path param 해석 — 실존 데이터 (GET /api/{prefix}s 첫 항목 id).
        # 파괴적 endpoint (cancel/terminate/leave/DELETE) 는 전용 리소스를
        # 생성해 그것을 대상으로 — 공유 리소스 파괴로 인한 TC 간 상태 간섭
        # 차단 (P2: run 04d5f79e TS-009→TS-011 연쇄 오염).
        ref_ids: dict[str, Any] = {}
        params = _PARAM_RE.findall(path)
        last_seg = path.rstrip("/").rsplit("/", 1)[-1].lower()
        destructive = method == "DELETE" or last_seg in _DESTRUCTIVE_SEGMENTS
        path_template = path
        if params:
            ref_ids = await _resolve_ref_ids(client, base, headers, params)
            if destructive and not auth_negative and headers:
                for p in params:
                    new_id = await _create_dedicated_resource(client, base, headers, p)
                    if new_id is not None:
                        ref_ids[p.lower()] = new_id
                        logger.info("api_exec_dedicated_resource",
                                    trace_id=trace_id, tc_id=tc_id,
                                    param=p, resource_id=new_id)
            for p in params:
                rid = ref_ids.get(p.lower(), 1)
                path = path.replace("{" + p + "}", str(rid))

        body = _build_body(tc) if method in {"POST", "PUT", "PATCH"} else None
        if body is not None:
            _uniquify_email_in_body(body, path, intent_negative, trace_id)
            bumped = _bump_past_dates(body, intent_negative)
            if bumped:
                logger.info("api_exec_date_bumped", trace_id=trace_id,
                            tc_id=tc_id, fields=bumped)
        url = f"{base}{path}"

        # 전제 상태 조성 (P2): '이미 ~된/중복' 류 negative 는 같은 호출을 1회
        # 선행해 충돌 상태에 도달시킨다 (예: 해지 후 재해지 → '이미 해지된
        # 회선' 오류가 기대 결과. run 04d5f79e: TS-009-TC-03 이 활성 주문을
        # 해지 '성공' 해버려 fail). 선행 호출 결과는 측정에 불포함.
        if intent_negative and any(k in then for k in _CONFLICT_HINTS):
            try:
                pre = await client.request(method, url, json=body, headers=headers)
                logger.info("api_exec_conflict_arranged", trace_id=trace_id,
                            tc_id=tc_id, arrange_status=pre.status_code)
            except Exception:
                pass

        try:
            resp = await client.request(method, url, json=body, headers=headers)
        except Exception as e:
            out["reason"] = f"호출 실패: {type(e).__name__}: {e}"
            logger.warning("api_exec_request_failed", trace_id=trace_id, tc_id=tc_id, error=str(e)[:120])
            return out

        # 422 자가치유 사다리 (positive 의도 한정) — TC values 의 필수 필드 누락은
        # 검증 대상이 아니라 요청 구성 결함이다 (run 04d5f79e: scheduled-change 가
        # new_plan_id/effective_date 중 하나만 보유 → 422). negative 의도는 4xx
        # 자체가 기대 결과라 치유 금지 (false-pass 채널 방지).
        healed_attempts = 0
        if not intent_negative and method in {"POST", "PUT", "PATCH"}:
            while resp.status_code == 422 and healed_attempts < 3:
                try:
                    errors = _field_errors_from_422(resp.json())
                except Exception:
                    errors = []
                if not errors or body is None:
                    break
                for loc_path, err_type, ctx in errors:
                    _apply_field_error(body, loc_path, err_type, ctx, ref_ids)
                healed_attempts += 1
                try:
                    resp = await client.request(method, url, json=body, headers=headers)
                except Exception as e:
                    out["reason"] = f"치유 재시도 호출 실패: {type(e).__name__}: {e}"
                    return out
            if healed_attempts:
                logger.info("api_exec_body_healed", trace_id=trace_id, tc_id=tc_id,
                            attempts=healed_attempts, final_status=resp.status_code)

        # 값 접지 재시도 (P2, positive 한정) — 4xx 의 원인이 'TC 값 vs SUT
        # 현재 상태' 불일치인 군집 (run 2464603c 실증): 400/409 는 body 값
        # (허용 목록 외 label, 이미 이용 중인 plan_id), 404 는 path id (계약
        # 없는 주문). SUT 에서 유효 후보를 조회해 값만 바꿔 재시도 — 검증
        # 의도 (then) 는 그대로, 값 선택만 상태-인지로.
        grounded: list[str] = []
        if not intent_negative:
            # ① 404 + path param → 컬렉션의 다른 id 후보 순회
            if resp.status_code == 404 and params:
                for p in params:
                    field = p if p.endswith("_id") else f"{p}_id"
                    cands = await _candidate_values(
                        client, base, headers, field, ref_ids.get(p.lower()), "")
                    for cand in cands:
                        trial_ids = dict(ref_ids)
                        trial_ids[p.lower()] = cand
                        trial_path = path_template
                        for q in params:
                            trial_path = trial_path.replace(
                                "{" + q + "}", str(trial_ids.get(q.lower(), 1)))
                        try:
                            trial = await client.request(
                                method, f"{base}{trial_path}", json=body, headers=headers)
                        except Exception:
                            continue
                        if trial.status_code != 404:
                            resp, url = trial, f"{base}{trial_path}"
                            ref_ids = trial_ids
                            grounded.append(f"path:{p}={cand}")
                            break
                    if resp.status_code != 404:
                        break
            # ② 400/409 + body → 동명 필드의 SUT 실측 값으로 교체
            if resp.status_code in (400, 409) and body:
                for field, current in list(body.items()):
                    if len(grounded) >= 3 or resp.status_code < 400:
                        break
                    if "password" in field.lower() or "email" in field.lower():
                        continue
                    cands = await _candidate_values(
                        client, base, headers, field, current,
                        url[len(base):] if url.startswith(base) else "")
                    for cand in cands:
                        trial_body = dict(body)
                        trial_body[field] = cand
                        try:
                            trial = await client.request(
                                method, url, json=trial_body, headers=headers)
                        except Exception:
                            continue
                        if trial.status_code < 400:
                            resp, body = trial, trial_body
                            grounded.append(f"body:{field}={cand}")
                            break
            if grounded:
                logger.info("api_exec_value_grounded", trace_id=trace_id,
                            tc_id=tc_id, replacements=grounded,
                            final_status=resp.status_code)

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
        if healed_attempts:
            out["healed_attempts"] = healed_attempts
        if not intent_negative and grounded:
            out["value_grounded"] = grounded

        # 판정 ⓪ observe 스펙 (P3) — 구조화 관찰 명세 보유 시 휴리스틱 해석
        # 대신 기계 실행. verdict = 전 observe 의 AND.
        api_observes = [
            o for o in (tc.get("observe") or [])
            if isinstance(o, dict) and o.get("kind") in _API_OBSERVE_KINDS
            and not _is_tautological_predicate(o.get("predicate"))
        ]
        if api_observes:
            obs_results: list[tuple[bool, str]] = []
            for o in api_observes:
                ok, why = await _eval_observe(
                    o, resp, response_body, body, snapshot_fetch, db_table)
                obs_results.append((ok, why))
            out["observe_results"] = [
                {"kind": o.get("kind"), "ok": ok, "reason": why}
                for o, (ok, why) in zip(api_observes, obs_results)
            ]
            all_ok = all(ok for ok, _ in obs_results)
            if resp.status_code >= 500 and not any(
                o.get("kind") == "http_status" for o in api_observes
            ):
                all_ok = False  # 5xx 는 status observe 부재 시에도 결함 신호
            out["verdict"] = "pass" if all_ok else "fail"
            out["reason"] = "observe: " + "; ".join(w for _, w in obs_results)[:280]
            return out

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

        # 판정 ② DB-계약형 (P1.5) — Act via API, Assert via DB 스냅샷.
        # 스냅샷은 호출자 주입 fetcher (캐시 우회 — Act 직후 신선 상태 필수).
        db_kind = _db_contract(then)
        if db_kind and snapshot_fetch and db_table:
            snapshot = None
            try:
                snapshot = await snapshot_fetch(db_table)
            except Exception as e:
                logger.warning("api_exec_db_snapshot_failed",
                               trace_id=trace_id, tc_id=tc_id, table=db_table,
                               error=str(e)[:120])
            ok, obs_reason = _observe_db(db_kind, snapshot, body)
            if db_kind == "exists":
                # 생성 계약은 Act 자체의 성공이 전제 — 5xx/기대 외 status 면 fail
                ok = ok and resp.status_code in _expected_statuses(then, intent_negative)
            if resp.status_code >= 500:
                ok = False
            out["verdict"] = "pass" if ok else "fail"
            out["reason"] = f"{obs_reason} (status {resp.status_code})"
            out["db_observation"] = {"kind": db_kind, "table": db_table, "ok": ok}
            return out
        if db_kind:
            # 계약은 DB 형인데 관찰 수단 부재 — status 기반으로 폴백하되 표식 남김
            out["db_observation"] = {"kind": db_kind, "table": db_table, "ok": None}

        # 판정 ③ 상태코드 계약
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
