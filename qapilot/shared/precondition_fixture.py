"""run-level 데이터 사전조건 fixture — Arrange via API, Act/Assert via UI.

배경 (run 544ab04d): TC 들이 참조하는 리소스 (예: /api/orders/{order_id}/...)
가 SUT 에 0건이면 깊은 흐름 TC 가 전부 '변경할 대상 없음' 으로 퇴화한다
(GET /api/orders == [] → 요금제 변경/예약 TS 전멸 + 타임아웃). 사전 상태
구축은 UI 가 아닌 API 로 하는 것이 실무 e2e 표준 구조다.

SUT 무관 동작 원리 (mini-bss 하드코딩 없음):
- 대상 리소스: 선택된 TC.api 들 중 path param (`{...}`) 을 가진 첫 segment
- 존재 확인: GET {base}/api/{resource} (Bearer token)
- 생성: POST {base}/api/{resource} 를 빈 body 로 시도 → FastAPI/DRF 류
  422 검증 응답의 missing/type 정보를 읽어 필드를 휴리스틱으로 채워 재시도
  (자가치유 body builder, 최대 4회). `*_id` 필드는 GET /api/{prefix}s 의
  첫 항목 id 를 참조 (실존 데이터 우선).
- 모든 실패는 graceful — fixture 가 못 만들면 TC 들이 기존대로 정직하게
  실패/스킵하고, 분류기가 ENV 계열로 표기한다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import httpx

from qapilot.shared.logger import get_logger

logger = get_logger(source="precondition_fixture")

# path 어딘가에 {param} 이 있으면 1차 리소스 segment 채택
# (/api/orders/{id}, /api/tier/brands/{code}/issue → orders, tier)
_PARAM_RESOURCE_RE = re.compile(r"/api/([a-zA-Z_]+)\S*\{")
# 중첩 스키마 (list[Model]) 는 단계적으로 드러난다:
# {} → missing items → [] → too_short → [{}] → missing items.0.plan_id → 값
_MAX_BODY_REPAIR = 6
_TIMEOUT = httpx.Timeout(10.0)


def _collect_param_resources(tc_apis: list[str]) -> list[str]:
    """path param 을 가진 api 들의 1차 리소스 segment (중복 제거, 순서 보존)."""
    seen: list[str] = []
    for api in tc_apis:
        m = _PARAM_RESOURCE_RE.search(str(api or ""))
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _guess_field_value(field: str, err_type: str, ref_ids: dict[str, Any]) -> Any:
    """422 검증 응답의 필드명/타입으로 값 휴리스틱."""
    f = field.lower()
    if f.endswith("_id"):
        return ref_ids.get(f, 1)
    if "date" in f:
        return (date.today() + timedelta(days=1)).isoformat()
    if "email" in f:
        return "fixture@qapilot.test"
    if "int" in err_type or "number" in err_type:
        return 1
    if "bool" in err_type:
        return True
    if "list" in err_type or "array" in err_type:
        return []
    return "qapilot-fixture"


async def _login(client: httpx.AsyncClient, base: str, account: dict) -> str | None:
    """일반적 로그인 endpoint 휴리스틱으로 token 획득."""
    for path in ("/api/auth/login", "/api/login", "/auth/login"):
        try:
            r = await client.post(
                f"{base}{path}",
                json={"email": account.get("email"), "password": account.get("password")},
            )
        except Exception:
            continue
        if r.status_code < 300:
            data = r.json() if r.content else {}
            token = data.get("token") or data.get("access_token")
            if token:
                return str(token)
    return None


async def _resolve_ref_ids(
    client: httpx.AsyncClient, base: str, headers: dict, missing_fields: list[str]
) -> dict[str, Any]:
    """`*_id` 필드 → GET /api/{prefix}s (또는 단수형) 첫 항목 id."""
    ref_ids: dict[str, Any] = {}
    for field in missing_fields:
        f = field.lower()
        if not f.endswith("_id"):
            continue
        prefix = f[:-3]
        for candidate in (f"{prefix}s", prefix):
            try:
                r = await client.get(f"{base}/api/{candidate}", headers=headers)
            except Exception:
                continue
            if r.status_code < 300:
                try:
                    items = r.json()
                except Exception:
                    continue
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    rid = items[0].get("id")
                    if rid is not None:
                        ref_ids[f] = rid
                        break
    return ref_ids


def _missing_fields_from_422(payload: Any) -> list[tuple[str, str]]:
    """FastAPI 422 detail → [(leaf_field, err_type)]. (호환용 — 평면 뷰)"""
    return [
        (str(loc[-1]), err_type)
        for loc, err_type, _ in _field_errors_from_422(payload)
    ]


def _field_errors_from_422(payload: Any) -> list[tuple[list, str, dict]]:
    """FastAPI 422 detail → [(body 이후 loc 경로, err_type, ctx)].

    중첩 스키마 (list[Model]) 의 loc=["body","items",0,"plan_id"] 까지 보존.
    """
    out: list[tuple[list, str, dict]] = []
    detail = (payload or {}).get("detail") if isinstance(payload, dict) else None
    for item in detail or []:
        if not isinstance(item, dict):
            continue
        loc = list(item.get("loc") or [])
        if len(loc) >= 2 and loc[0] == "body":
            out.append((loc[1:], str(item.get("type") or ""), item.get("ctx") or {}))
    return out


def _guess_typed_value(field: str, err_type: str, ctx: dict, ref_ids: dict[str, Any]) -> Any:
    """err_type/ctx 우선 타입 휴리스틱 (중첩 경로 leaf 용)."""
    if "list" in err_type:
        return []
    if "dict" in err_type or "model" in err_type:
        return {}
    expected = str(ctx.get("expected") or "")
    if expected:
        # Literal/enum: ctx.expected = "'NONE', '12' or '24'" → 첫 허용값
        m = re.search(r"'([^']+)'", expected)
        if m:
            return m.group(1)
    return _guess_field_value(field, err_type, ref_ids)


def _apply_field_error(
    body: dict, loc_path: list, err_type: str, ctx: dict, ref_ids: dict[str, Any]
) -> None:
    """loc 경로를 따라 body 를 패치 — 중간 컨테이너 (dict/list) 자동 생성.

    too_short (min_length) 는 리스트에 빈 객체 추가 — 다음 422 가 그 객체의
    필수 필드를 알려준다 (자가치유 사다리).
    """
    target: Any = body
    for i, key in enumerate(loc_path[:-1]):
        nxt = loc_path[i + 1]
        want_list = isinstance(nxt, int)
        if isinstance(key, int):
            if not isinstance(target, list):
                return
            while len(target) <= key:
                target.append([] if want_list else {})
            target = target[key]
        else:
            if not isinstance(target, dict):
                return
            if key not in target or not isinstance(target[key], (dict, list)):
                target[key] = [] if want_list else {}
            target = target[key]

    leaf = loc_path[-1]
    if "too_short" in err_type:
        current = target[leaf] if isinstance(target, dict) and leaf in target else None
        if isinstance(current, list):
            current.append({})
        elif isinstance(target, dict):
            target[leaf] = [{}]
        return
    value = _guess_typed_value(str(leaf), err_type, ctx, ref_ids)
    if isinstance(leaf, int):
        if isinstance(target, list):
            while len(target) <= leaf:
                target.append({})
            target[leaf] = value
        return
    if isinstance(target, dict):
        target[leaf] = value


async def ensure_resource_preconditions(
    base_url: str,
    test_account: dict,
    tc_apis: list[str],
    trace_id: str = "",
) -> dict[str, str]:
    """선택 TC 들이 참조하는 리소스의 최소 1건 존재를 보장한다.

    Returns:
        {resource: "exists" | "created" | "failed:<reason>"} — 로그/검증용.
    """
    results: dict[str, str] = {}
    resources = _collect_param_resources(tc_apis)
    if not resources:
        return results
    base = (base_url or "").rstrip("/")
    if not base or not test_account:
        return results

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _login(client, base, test_account)
        if not token:
            logger.warning("precondition_login_failed", trace_id=trace_id, base=base)
            return {r: "failed:login" for r in resources}
        headers = {"Authorization": f"Bearer {token}"}

        for res in resources:
            list_url = f"{base}/api/{res}"
            try:
                r = await client.get(list_url, headers=headers)
                if r.status_code < 300:
                    items = r.json()
                    if isinstance(items, list) and len(items) > 0:
                        results[res] = "exists"
                        logger.info(
                            "precondition_resource_exists",
                            trace_id=trace_id, resource=res, count=len(items),
                        )
                        continue
                elif r.status_code in (404, 405):
                    results[res] = f"failed:list_{r.status_code}"
                    continue
            except Exception as e:
                results[res] = f"failed:list_{type(e).__name__}"
                continue

            # 0건 → 자가치유 생성 시도
            body: dict[str, Any] = {}
            created = False
            for attempt in range(_MAX_BODY_REPAIR):
                try:
                    cr = await client.post(list_url, json=body, headers=headers)
                except Exception as e:
                    results[res] = f"failed:create_{type(e).__name__}"
                    break
                if cr.status_code < 300:
                    created = True
                    results[res] = "created"
                    logger.info(
                        "precondition_resource_created",
                        trace_id=trace_id, resource=res,
                        attempts=attempt + 1, body_fields=sorted(body.keys()),
                    )
                    break
                if cr.status_code == 422:
                    try:
                        errors = _field_errors_from_422(cr.json())
                    except Exception:
                        errors = []
                    if not errors:
                        results[res] = "failed:create_422_unparsable"
                        break
                    leaf_names = [
                        str(loc[-1]) for loc, _, _ in errors
                        if not isinstance(loc[-1], int)
                    ]
                    ref_ids = await _resolve_ref_ids(client, base, headers, leaf_names)
                    for loc_path, err_type, ctx in errors:
                        _apply_field_error(body, loc_path, err_type, ctx, ref_ids)
                    continue
                results[res] = f"failed:create_{cr.status_code}"
                break
            if not created and res not in results:
                results[res] = "failed:create_max_repair"
            if not created:
                logger.warning(
                    "precondition_resource_create_failed",
                    trace_id=trace_id, resource=res, reason=results.get(res),
                )

    return results
