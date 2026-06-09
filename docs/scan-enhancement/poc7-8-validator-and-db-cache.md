# PoC 7 + 8 — TVValidator + DBTool TTL cache (유빈 agent 검증 helper)

> 본인 영역 (주환). 유빈 TC/TV agent 가 호출할 검증 utility.
> PoC 6 (다른 영역 routes/schemas) 는 추후 작업 — 본 PoC 7/8 은 독립 동작.

---

## 1. 한 줄 요약

| | |
|---|---|
| **PoC 7 목적** | TV (test value) 가 schema/format/DB 의도와 맞는지 검증 (`TVValidator`) |
| **PoC 8 목적** | DBTool snapshot 의 process-local TTL cache (TVValidator 의 DB 입력 helper) |
| **재사용** | PoC 4 의 LRU 패턴 + TTL 추가, DBTestTool 의 기존 `_get_snapshot/_get_tables` |
| **검증** | PoC 7 = 50/50, PoC 8 = 18/18 PASS (총 68 케이스, mock 격리) |
| **상태** | ✅ commit (브랜치 `feat/me/scan-enhancement-foundation`) |

---

## 2. 흐름도 (유빈 agent 의 검증 사이클)

```text
[유빈 TC/TV generator agent]
   │  ① TV 생성 (LLM)
   │     tv_field = {"name": "email", "value": "test@example.com", "source": "llm"}
   │
   │  ② 메타데이터 + DB 입력 준비 (본인 helper 들)
   ├─→ load_metadata_index(sid, "backend", "schemas")     [PoC 4]
   │     → BackendSchemasIndex (request_schemas / db_models)
   ├─→ get_db_snapshot_cached(sid, "customers")           [PoC 8]
   │     → {"table": "customers", "rows": [...]}
   │     (TTL 60초 — 같은 trace 안 반복 호출 cost 1회)
   │
   │  ③ TVValidator 호출                                   [PoC 7]
   ▼
[TVValidator.validate]
   ├─ schema 일치  — schemas.request_schemas["SignupRequest"].fields 매칭
   ├─ type 매칭    — EmailStr/str/int/...
   ├─ format       — email_format / iso_date / min_length / regex / ...
   ├─ db_existence — scenario_intent.expects_existing_in_db
   └─ db_absence   — scenario_intent.expects_absent_in_db
   │
   ▼ ValidationResult(valid: bool, checks: [...], reasons: [...])
   │
[유빈 agent]
   ├─ valid=True  → TC 완성 + TV pool 등록
   └─ valid=False → reasons 를 LLM prompt 에 피드백 + 재시도
                    (max_retries 결정은 호출자 책임)
```

---

## 3. PoC 7 — TVValidator

### 3.1 ValidationResult

```python
@dataclass
class ValidationResult:
    valid: bool                              # 모든 check pass 여부
    checks: list[ValidationCheck]            # 모든 check 의 상세
    reasons: list[str]                       # valid=False 일 때 failed checks 의 detail
```

`reasons` 는 사람이 읽기 쉬운 한국어 — 유빈 agent 가 LLM 재시도 prompt 에 그대로
컨텍스트로 주입 가능 (예: `"email 형식 FAIL: 'bad'"` → LLM 이 다음 시도에서 유효 email).

### 3.2 검증 종류 (도메인 무관)

| kind | 활성 조건 | 검증 내용 |
|---|---|---|
| `schema` | `schemas` + `schema_name` 둘 다 제공 | `tv_field.name` 이 schemas 의 fields 에 있나? |
| `type` | schema 검증 통과 + field.type 존재 | Python 타입 매칭 (str/int/float/bool/date/uuid/dict/list) |
| `format` | schema 검증 통과 + field.validators 존재 | email_format / iso_format / uuid_format / min_length / max_length / regex / required |
| `db_existence` | `db_snapshot` + `scenario_intent.expects_existing_in_db=True` | snapshot row 에 같은 (name, value) 존재 |
| `db_absence` | `db_snapshot` + `scenario_intent.expects_absent_in_db=True` | snapshot row 에 같은 (name, value) 부재 |

### 3.3 graceful 정책 (PoC)

- `schemas=None` → schema/type/format 검증 skip
- `db_snapshot=None` 또는 `scenario_intent` 에 expects_* 없음 → DB 검증 skip
- 알 수 없는 validator kind / type → 관대 pass (PoC, 향후 strict 모드)
- 모든 검증 skip → `valid=True` (검증 안 한 게 invalid 라고 단정 못함)

### 3.4 schemas 의 출처 = PoC 6 (추후) 또는 caller 가 직접 dict 주입

PoC 7 는 `schemas` 인자로 단순 dict 받음. 향후 PoC 6 에서 `BackendSchemasIndex` 추출기
완성되면 `load_metadata_index("backend", "schemas")` 결과를 그대로 주입.
지금은 caller 가 dict 직접 구성 가능 (예: mini-bss-lite 의 Pydantic 모델을 수동 매핑).

---

## 4. PoC 8 — DBTool snapshot TTL cache

### 4.1 Public API

```python
from qapilot.shared.db_state import (
    list_db_tables_cached,
    get_db_snapshot_cached,
    clear_db_cache,
    cache_stats,
)

tables = await list_db_tables_cached(service_id)                  # TTL 300s
snap = await get_db_snapshot_cached(service_id, "customers")      # TTL 60s
```

### 4.2 cache 정책

| 항목 | 값 |
|---|---|
| 구조 | per-process dict + `threading.Lock` (thread-safe) |
| key | `("tables", service_id)` / `("snapshot", service_id, table)` |
| TTL | tables 300s, snapshot 60s (caller override 가능) |
| 실패 cache | **안 함** — DB 호출 실패 시 None 반환, 다음 호출에서 재시도 |
| 격리 | service_id 단위 — multi-service 시 cache 충돌 없음 |

### 4.3 향후 multi-service 확장

현재 DBTestTool 은 단일 `QAPILOT_SUT_DB_URL` 환경변수 가정. multi-service 시점에:
1. DBTestTool 의 `_module_url()` 을 service 별 분기로 확장 (격차 12 후속)
2. 본인 cache 의 `service_id` 키가 그대로 격리 보장 (코드 변경 없음)

회의 결정 5 의 옵션 B (`/db/query` endpoint) 도입은 별도 — E 영역 (kshyun) 협업 후속.

---

## 5. 통합 예시 (유빈 agent 의 코드 — 가상)

```python
from qapilot.shared.scan_storage import load_metadata_index    # PoC 4
from qapilot.shared.db_state import get_db_snapshot_cached    # PoC 8
from qapilot.shared.tv_validator import TVValidator           # PoC 7

validator = TVValidator()

async def generate_and_validate_tv(service_id, tc, retry_max=3):
    schemas = load_metadata_index(service_id, "backend", "schemas")
    table = _infer_table_from_tc(tc)
    snap = await get_db_snapshot_cached(service_id, table)
    rows = snap["rows"] if snap else None

    for attempt in range(retry_max):
        tv = await llm.generate_tv(tc, context={"schemas": schemas, "rows": rows[:5] if rows else []})
        result = validator.validate(
            tv_field={"name": tv["field"], "value": tv["value"]},
            scenario_intent={
                "expects_existing_in_db": tc.get("uses_existing_data", False),
                "expects_absent_in_db": tc.get("requires_new_data", False),
            },
            db_snapshot=rows,
            schemas=schemas,
            schema_name=tc.get("schema_name"),
        )
        if result.valid:
            return tv
        # 재시도 — reasons 를 LLM prompt 에 피드백
        feedback = "\n".join(f"- {r}" for r in result.reasons)
        tc["validation_feedback"] = feedback

    raise RuntimeError(f"TV 검증 {retry_max} 회 실패: {result.reasons}")
```

---

## 6. 단위 테스트 매트릭스

### 6.1 PoC 7 (`tests/test_tv_validator.py` — 50/50 PASS)
- format validators 22 케이스 (email/iso_date/uuid/min/max/regex/required, parametrize)
- 타입 매칭 14 케이스 (str/int/bool/float/date/uuid/dict/list/unknown, parametrize)
- 통합 14 케이스 (no schema, missing name, schema match/miss, type mismatch, format fail, DB existence/absence, intent 없음 skip, full pipeline, db_models lookup, reasons 한국어)

### 6.2 PoC 8 (`tests/test_db_state.py` — 18/18 PASS)
- `_TTLCache` 5 케이스 (hit/expiry/missing/clear/stats)
- `list_db_tables_cached` 5 케이스 (cache hit, service 격리, 실패 None, 실패 non-cache, TTL 만료)
- `get_db_snapshot_cached` 6 케이스 (cache hit, table 격리, service 격리, 실패 None, 빈 args, TTL 만료)
- cache 관리 2 케이스 (stats, clear)

mock 격리 — 실 DB/HTTP 의존 없이 cache 동작 검증.

---

## 7. 격차 매핑 (PoC 0 의 8 격차)

| 격차 | PoC 7/8 의 기여 |
|---|---|
| **A-1 오라클** | TVValidator 가 schema/format/DB 의도로 LLM 출력의 오라클 역할 |
| **A-2 도메인 hardcoded** | format/type 매칭 = RFC/표준 (도메인 무관). validator 코드에 SUT 도메인 키워드 0 |
| **A-3 런타임 상태** | TVValidator 의 DB 존재성 검증 + DB cache (TTL) → TC-01 가 만든 row 가 TC-02 에서 보임 |
| **B-3 캐시** | DB snapshot TTL cache (PoC 4 의 LRU 패턴 확장) |
| **B-4 결정성** | TVValidator 결정적 (LLM 호출 없음, 순수 규칙) |

---

## 8. PoC 6 (추후 작업) — 본 PoC 와의 관계

PoC 6 (`frontend.routes` + `backend.schemas` 추출기) 완성 시:
- `load_metadata_index(sid, "backend", "schemas")` → 자동으로 BackendSchemasIndex 반환
- PoC 7 의 `schemas` 인자에 그대로 주입 가능
- 현재 단계 = caller 가 직접 dict 구성 (mini-bss-lite Pydantic 수동 매핑) 가능

PoC 6 늦어져도 본 PoC 7/8 은 독립 동작.
