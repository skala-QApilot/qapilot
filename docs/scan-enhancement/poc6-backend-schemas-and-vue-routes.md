# PoC 6 — backend.schemas + frontend.routes 추출기 (4영역 완성)

> 데이터 layer 담당 (주환). 4 영역 (selectors/routes/schemas/patterns) 의 마지막 2 영역 구현.

---

## 1. 한 줄 요약

| | |
|---|---|
| **PoC 6.A** | Pydantic BaseModel + SQLAlchemy Mapped/Column → `backend.schemas` |
| **PoC 6.B** | Vue Router `createRouter({routes: [...]})` → `frontend.routes` |
| **도구** | tree-sitter-python (PoC 6.A) + tree-sitter-javascript/typescript (PoC 6.B) — 기존 의존성 |
| **검증** | 50/50 단위 PASS + 실 환경 (mini-bss-lite) end-to-end |
| **상태** | ✅ commit (브랜치 `feat/juhwan/scan-enhancement-foundation`) |

---

## 2. 4영역 완성

| kind | sub_kind | 추출기 | 상태 |
|---|---|---|---|
| frontend | **selectors** | `vue_sfc_parser` (PoC 2) | ✅ |
| frontend | **routes** | `vue_router_parser` (PoC 6.B) | ✅ |
| backend | **schemas** | `backend_schema_parser` (PoC 6.A) | ✅ |
| sut_tests | **patterns** | `pytest_ast_parser` (PoC 5) + `llm_pattern_classifier` (PoC 5.1) | ✅ |

다른 framework (React JSX / Playwright / Cypress / Jest) 확장 = 후속.

---

## 3. PoC 6.A — backend.schemas

### 3.1 추출 분기

| 상속 base | 분류 |
|---|---|
| `BaseModel` (Pydantic) + 클래스명 끝이 Response/Out/Reply/Result | **response_schemas** |
| `BaseModel` 외 | **request_schemas** |
| `Base` 또는 `DeclarativeBase` (SQLAlchemy) | **db_models** |

### 3.2 Pydantic 필드 추출

```python
class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=r"^.+@.+$")
    password: str = Field(min_length=8)
    name: str
    birth_date: date
    consent: bool | None = None
```

→ 추출:
| name | type | required | nullable | validators | sensitive |
|---|---|---|---|---|---|
| email | str | True | False | min_length=3, max_length=255, regex=`^.+@.+$` | False |
| password | str | True | False | min_length=8 | **True** (휴리스틱) |
| name | str | True | False | — | False |
| birth_date | date | True | False | — | False |
| consent | bool | False | True | — | False |

required 결정 (Pydantic 의 미묘한 case 처리):
- `Field(...)` (ellipsis) → required
- `Field(min_length=3, ...)` (default keyword 없음) → required
- `Field(default=X)` 또는 `Field(default_factory=X)` → optional
- `name: str` (assignment 없음) → required
- `consent: bool | None = None` → optional + nullable

raw string regex (`r"..."`) 도 정확 추출 (`_strip_string_literal` 헬퍼).

### 3.3 SQLAlchemy 컬럼 추출

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
```

→ DbModel:
- `table_name="users"` (`__tablename__` 추출)
- columns: id(int, primary_key=True), email(str, unique=True, nullable=False), password_hash(str, sensitive=True)

`Mapped[int | None]` 의 `| None` → `nullable=True` 자동 매핑.
legacy `Column(...)` 도 같은 패턴.

### 3.4 mini-bss-lite 실 환경 검증

| 파일 | 추출 |
|---|---|
| `backend/app/schemas.py` | request=10, response=22 (Out 접미사 자동 분류) |
| `backend/app/models.py` | db_models=13 (Customer/Session/Plan/Order/...) |

전체: 45 schema record. SignupRequest 의 email 은 min_length/max_length/regex 3 validator + required=True 정확.

---

## 4. PoC 6.B — frontend.routes

### 4.1 지원 패턴

```javascript
// Vue Router 3/4 공통
const routes = [
  { path: '/dashboard', component: Dashboard, meta: { requiresAuth: true } },
  { path: '/plans/:id', component: PlanDetail },
  { path: '/old', redirect: '/' },
]

createRouter({ history: createWebHistory(), routes })
```

또는 인라인:
```javascript
createRouter({ routes: [{ path: '/foo', component: Foo }] })
```

### 4.2 추출 정보

| 필드 | 출처 |
|---|---|
| `path` | route object 의 `path` 키 |
| `component_name` | `component` 키의 identifier (e.g. `Dashboard`) |
| `params` | path 의 `:name` 패턴 (optional `:slug?` 도) |
| `guards` | `meta.requiresAuth` → `requireAuth`, `meta.requiresRole` → `requireRole:<role>` |
| `meta` | 그대로 dict 보존 |
| `redirects_when_authed` | `redirect: 'string'` (함수 redirect 는 보존 X) |

### 4.3 mini-bss-lite 실 환경 검증

```text
16 routes 추출:
/                            redirect 함수 → component 없음
/dashboard                   Dashboard               guards: requireAuth
/signup, /login              Signup, Login           (public)
/plans                       Plans                   (public)
/plans/:id                   PlanDetail              params: id
/order/new                   OrderNew                guards: requireAuth
/usage, /billing, /orders    각 component            guards: requireAuth
/orders/:id                  OrderDetail             params: id, guards: requireAuth
/notices, /notices/:id       각 component            (public, /notices/:id 는 params: id)
/profile, /membership        각 component            guards: requireAuth
/membership/brands/:code     MembershipBrandDetail   params: code, guards: requireAuth
```

총 16 record, params 3개 (id × 3, code × 1), guards 9개 (requireAuth) 정확.

---

## 5. PoC 6 → 3 → 4 → 7 통합 end-to-end

```text
[PoC 6.A] backend_schema_parser (schemas.py + models.py)
        → BackendSchemasIndex (45 records)
        │
[PoC 6.B] vue_router_parser (router/index.js)
        → FrontendRoutesIndex (16 records)
        │
        ▼
[PoC 3] upsert_metadata_index — 각각 S3 PUT + DB INSERT (sub_kind="schemas" / "routes")  ✅
        │
        ▼
[PoC 4] load_metadata_index — 둘 다 정확 조회 (request=10, db_models=13, routes=16)     ✅
        │
        ▼
[PoC 7] TVValidator.validate(
            tv_field={"name": "email", "value": "test@example.com"},
            schemas=loaded_bsi, schema_name="SignupRequest",
        )
        → valid=True (schema + type + 3 format validators 모두 pass)                      ✅

        TVValidator.validate(
            tv_field={"name": "email", "value": "bad"},
            schemas=loaded_bsi, schema_name="SignupRequest",
        )
        → valid=False, reason="regex(^[^@\s]+@[^@\s]+\.[^@\s]+$) FAIL"                    ✅
```

4영역 + 저장/조회/검증 사이클 모두 실 데이터로 정합 확인.

---

## 6. 단위 테스트 매트릭스 (총 50/50 PASS)

### 6.1 backend_schema_parser (`tests/test_backend_schema_parser.py` — 25 PASS)
- helpers: `_is_response_class` 5개, `_strip_string_literal` 3개 (raw string 포함), `_parse_pydantic_field_args` 2개, `_sqla_type_from_call` 1개
- Pydantic 추출 8개: count, response 분기, validators, required 정확화 (3 case: typed only, Field with kwargs, None default), model_config 제외
- SQLAlchemy 추출 4개: count, table_name, columns 속성, FK column
- 메타 + 에러 5개

### 6.2 vue_router_parser (`tests/test_vue_router_parser.py` — 25 PASS)
- helpers: `_strip_quotes` 4개 (double/single/backtick/unwrapped), `_extract_params` 4개 (no/single/multi/optional)
- 통합 9개: count, component, params, guards (requireAuth/requireRole/none), redirect, meta dict, extracted_from
- 메타 + 에러 8개 (path 없는 fixture, missing file, non-js/ts, inline createRouter)

mock 격리 — 실 S3/DB 의존 없이 추출 + Pydantic 모델 매핑 검증.

---

## 7. 본 데이터 layer 데이터 layer 완성 — 유빈 agent 의 4 public API

```python
from qapilot.shared.scan_storage import load_metadata_index, load_source
from qapilot.shared.db_state import get_db_snapshot_cached
from qapilot.shared.tv_validator import TVValidator

# 4 영역 모두 호출 가능
selectors = load_metadata_index(sid, "frontend",  "selectors")   # PoC 2
routes    = load_metadata_index(sid, "frontend",  "routes")      # PoC 6.B  ← 신규
schemas   = load_metadata_index(sid, "backend",   "schemas")     # PoC 6.A  ← 신규
patterns  = load_metadata_index(sid, "sut_tests", "patterns")    # PoC 5 + 5.1

snap   = await get_db_snapshot_cached(sid, "customers")           # PoC 8
result = TVValidator().validate(tv_field, intent, snap["rows"], schemas, "SignupRequest")  # PoC 7
code   = load_source(sid, sha, "auth.py", line_start=42, line_end=58)  # PoC 4
```

---

## 8. 후속 (PoC 6 의 framework 확장 — 추가 작업 가능)

후속 단위로 framework 확장 가능:

| 확장 | 추출기 | 도구 |
|---|---|---|
| React JSX selectors | `react_jsx_parser.py` | `@babel/parser` (vue-bridge 패턴 차용, Node subprocess) |
| React Router routes | `react_router_parser.py` | tree-sitter-typescript |
| Playwright e2e patterns | `playwright_parser.py` | tree-sitter-typescript |
| Cypress patterns | `cypress_parser.py` | tree-sitter-javascript |
| Jest / Vitest patterns | `jest_parser.py` | tree-sitter-typescript |

vue_router_parser 가 이미 JS+TS 둘 다 지원하므로, React Router 추출기는 80% 재사용 가능.

다른 SUT (kshyun Spring service 등) 의 `@RestController` + `@Entity` 추출은 Java 별도 (tree-sitter-java, 의존성 있음) → 후속 단위.
