# Metadata Schema 명세 — 4 영역 상세

> 본 문서는 `metadata_indices` 테이블에 저장되는 4 영역의 JSON schema 명세다.
> Pydantic model 은 `qapilot/shared/metadata_schemas.py` 에 구현.

---

## 0. 공통 규칙

### 0.1 모든 영역의 record 가 가지는 공통 필드
```json
{
  "extracted_from": {
    "file": "frontend/src/pages/Signup.vue",
    "line_start": 12,
    "line_end": 45,
    "commit_sha": "76ff013448..."
  },
  "confidence": 1.0,
  "extraction_method": "ast" | "llm" | "hybrid"
}
```

| 필드 | 의미 | 값 |
|---|---|---|
| `extracted_from` | 원본 추적 (B-4 결정성 + 재검증 가능) | file/line/sha |
| `confidence` | 추출 신뢰도 | `1.0` (AST) / `0.85` (LLM 의미 라벨) / `0.65` (LLM 추론) |
| `extraction_method` | 추출 방법 | `ast` / `llm` / `hybrid` |

### 0.2 결정성 보장
- AST 추출 = deterministic by definition (같은 input → 같은 output)
- LLM 추출 = `seed = hash(service_id + commit_sha + file_path + sub_kind)` 고정
- 같은 SHA → 같은 메타데이터 결과 (`(commit_sha, file_path)` cache 키)

---

## 1. `frontend.selectors` — 페이지별 testid 카탈로그

### 1.1 추출 대상
- Vue: `<input data-testid="email">` / `<button data-testid="signup-submit">`
- React: `data-testid="..."` 또는 `data-test-id`
- Angular: `data-testid` 또는 `[attr.data-testid]`

### 1.2 schema

```json
{
  "kind": "frontend",
  "sub_kind": "selectors",
  "service_id": "9f2a7d4f-...",
  "commit_sha": "76ff013448...",
  "by_route": {
    "/signup": {
      "inputs": [
        {
          "testid": "email",
          "html_type": "email",
          "required": true,
          "v_model": "form.email",
          "label": "이메일",
          "placeholder": "example@email.com",
          "validators": ["email", "required"],
          "extracted_from": {"file": "frontend/src/pages/Signup.vue", "line_start": 22, "line_end": 30, "commit_sha": "..."},
          "confidence": 1.0,
          "extraction_method": "ast"
        }
      ],
      "buttons": [
        {
          "testid": "signup-submit",
          "html_type": "submit",
          "form_role": "submit",
          "disabled_when": {
            "expr": "loading || (isMinor && !form.guardian_consent)",
            "semantic": "로딩 중이거나 미성년 동의 미체크 시 비활성",
            "confidence_for_semantic": 0.65,
            "extraction_method_for_semantic": "llm"
          },
          "extracted_from": {...},
          "confidence": 1.0,
          "extraction_method": "ast"
        }
      ],
      "outputs": [
        {
          "testid": "signup-error",
          "html_tag": "div",
          "v_if": "error",
          "semantic_kind": "error_toast",
          "semantic_purpose": "회원가입 실패 시 에러 메시지 표시",
          "extracted_from": {...},
          "confidence": 0.85,
          "extraction_method": "hybrid"
        },
        {
          "testid": "signup-success-toast",
          "html_tag": "div",
          "v_if": "success",
          "semantic_kind": "success_toast",
          "semantic_purpose": "회원가입 성공 시 표시",
          "extracted_from": {...},
          "confidence": 0.85,
          "extraction_method": "hybrid"
        }
      ],
      "dynamic": [
        {
          "testid": "guardian-consent",
          "html_tag": "input",
          "html_type": "checkbox",
          "v_if": "isMinor",
          "semantic_purpose": "미성년자일 때 표시되는 법정대리인 동의 체크박스",
          "extracted_from": {...},
          "confidence": 0.65,
          "extraction_method": "llm"
        }
      ]
    }
  }
}
```

### 1.3 추출 방법 분리
| 필드 | AST | LLM |
|---|---|---|
| `testid`, `html_type`, `html_tag`, `v_model`, `v_if`, `required` | ✅ | ❌ |
| `disabled_when.expr` (raw expression) | ✅ | ❌ |
| `disabled_when.semantic` (자연어 해석) | ❌ | ✅ |
| `semantic_kind` (error_toast/success_toast/...) | ❌ | ✅ |
| `semantic_purpose` (자연어 설명) | ❌ | ✅ |

### 1.4 활용 (유빈 agent 가 이렇게 사용)
- TC.given/when/then 의 selector 확정 시 `by_route["/signup"].inputs[].testid` 직접 사용
- "에러 메시지가 표시된다" 라는 then 절 → `outputs[].semantic_kind="error_toast"` 매칭 → `signup-error` testid 사용
- 동적 element (`guardian-consent`) → TC 가 미성년 시나리오일 때만 사용

---

## 2. `frontend.routes` — Vue Router / React Router / Next.js 추출

### 2.1 추출 대상
- Vue Router: `router/index.js` 의 `{path, component, meta}` 배열
- React Router: `<Route path="..." element={...}>`
- Next.js: `app/` 또는 `pages/` 디렉토리 구조

### 2.2 schema

```json
{
  "kind": "frontend",
  "sub_kind": "routes",
  "service_id": "9f2a7d4f-...",
  "commit_sha": "76ff013448...",
  "routes": [
    {
      "path": "/signup",
      "component_file": "frontend/src/pages/Signup.vue",
      "component_name": "Signup",
      "guards": [],
      "redirects_when_authed": "/dashboard",
      "meta": {"requires_auth": false, "title": "회원가입"},
      "params": [],
      "query_params": [{"name": "ref", "optional": true}],
      "extracted_from": {"file": "frontend/src/router/index.js", "line_start": 25, "line_end": 28, "commit_sha": "..."},
      "confidence": 1.0,
      "extraction_method": "ast"
    },
    {
      "path": "/orders/:order_id",
      "component_file": "frontend/src/pages/OrderDetail.vue",
      "params": [{"name": "order_id", "type": "string"}],
      "guards": [{"name": "requireAuth", "kind": "beforeEnter"}],
      "extracted_from": {...},
      "confidence": 1.0,
      "extraction_method": "ast"
    }
  ],
  "default_redirects": {
    "/": "/dashboard",
    "*": "/404"
  }
}
```

### 2.3 활용
- TC 시작 시 navigate 의 actual path 확정 (`path: "/signup"` 직접 사용)
- 동적 param (`:order_id`) 가 있는 route 의 경우 TV 에서 actual id resolve 필요 → DBTool snapshot 활용
- `guards: [requireAuth]` → 시나리오의 precondition (사전 로그인) 추론

---

## 3. `backend.schemas` — Pydantic / SQLAlchemy / Prisma / TypeScript interface

### 3.1 추출 대상
- FastAPI Pydantic: `class SignupRequest(BaseModel):`
- SQLAlchemy: `class Customer(Base):`
- Prisma: `model Customer { ... }`
- TypeScript: `interface UserSchema { ... }`

### 3.2 schema

```json
{
  "kind": "backend",
  "sub_kind": "schemas",
  "service_id": "9f2a7d4f-...",
  "commit_sha": "76ff013448...",
  "request_schemas": {
    "SignupRequest": {
      "fields": [
        {
          "name": "email",
          "type": "EmailStr",
          "required": true,
          "validators": [
            {"kind": "email_format"},
            {"kind": "max_length", "value": 254}
          ],
          "examples": ["user@example.com"],
          "extracted_from": {"file": "backend/app/schemas/auth.py", "line_start": 12, "line_end": 14, "commit_sha": "..."},
          "confidence": 1.0,
          "extraction_method": "ast"
        },
        {
          "name": "password",
          "type": "str",
          "required": true,
          "validators": [
            {"kind": "min_length", "value": 8},
            {"kind": "regex", "value": "^(?=.*[A-Z])(?=.*[0-9])(?=.*[!@#$%^&*]).+$"}
          ],
          "sensitive": true,
          "extracted_from": {...},
          "confidence": 1.0,
          "extraction_method": "ast"
        },
        {
          "name": "birth_date",
          "type": "date",
          "required": true,
          "validators": [{"kind": "iso_format"}],
          "extracted_from": {...},
          "confidence": 1.0,
          "extraction_method": "ast"
        }
      ]
    }
  },
  "response_schemas": {
    "SignupResponse": {
      "status_codes": [
        {"code": 201, "description": "회원가입 성공", "body": {"id": "int", "email": "str", "created_at": "datetime"}},
        {"code": 409, "description": "이메일 중복", "body": {"detail": "Email already registered"}, "extracted_from_handler": "backend/app/routers/auth.py:42"},
        {"code": 400, "description": "미성년자 동의 없음", "body": {"detail": "미성년자 가입 시 법정대리인의 동의가 필요합니다."}, "extracted_from_handler": "backend/app/routers/auth.py:55"}
      ]
    }
  },
  "db_models": {
    "Customer": {
      "table_name": "customers",
      "columns": [
        {"name": "id", "type": "int", "primary_key": true, "auto_increment": true},
        {"name": "email", "type": "str", "unique": true, "nullable": false},
        {"name": "password_hash", "type": "str", "nullable": false},
        {"name": "birth_date", "type": "date", "nullable": false},
        {"name": "created_at", "type": "datetime", "default": "now()"}
      ],
      "extracted_from": {"file": "backend/app/models/customer.py", ...},
      "confidence": 1.0,
      "extraction_method": "ast"
    }
  }
}
```

### 3.3 활용
- TV 생성 시 `request_schemas.SignupRequest.fields[].validators` 충족 보장 (password 8자 이상 등)
- TV 검증 시 `db_models.Customer.columns` 의 unique 제약 → 시나리오 의도와 매칭 (negative "이미 가입된" = DB 에 존재해야)
- `response_schemas.status_codes` → 시나리오 then 절의 "409 Email already registered" 와 정확 매칭 (A-1 격차 일부 해소)

---

## 4. `sut_tests.patterns` — SUT 의 기존 테스트 코드 패턴

### 4.1 추출 대상
- 디렉토리 우선순위: `tests/e2e/` > `e2e/` > `tests/integration/` > `tests/` > `spec/`
- 파일 형식: `.spec.ts/.spec.js/.test.ts/.test.js/.py`
- Playwright / Cypress / Pytest / Jest 등 framework agnostic

### 4.2 schema

```json
{
  "kind": "sut_tests",
  "sub_kind": "patterns",
  "service_id": "9f2a7d4f-...",
  "commit_sha": "76ff013448...",
  "patterns": [
    {
      "pattern_kind": "auth-setup",
      "framework": "playwright",
      "file": "tests/e2e/auth.spec.ts",
      "line_start": 10,
      "line_end": 30,
      "snippet": "await page.goto('/login');\nawait page.fill('[data-testid=email]', 'admin@test.com');\nawait page.fill('[data-testid=password]', process.env.TEST_PASSWORD);\nawait page.click('[data-testid=login-submit]');\nconst token = await page.evaluate(() => localStorage.getItem('token'));",
      "purpose": "JWT 로그인 후 localStorage 에서 token 추출 → 후속 API 요청의 Authorization header 에 사용",
      "uses_env_vars": ["TEST_PASSWORD"],
      "uses_data_testid": ["email", "password", "login-submit"],
      "extraction_method": "hybrid",
      "confidence": 0.85
    },
    {
      "pattern_kind": "db-seed",
      "framework": "pytest",
      "file": "tests/conftest.py",
      "line_start": 25,
      "line_end": 45,
      "snippet": "@pytest.fixture\ndef seed_customer(db):\n    customer = Customer(email='seed@test.com', password_hash='...', birth_date='2000-01-01')\n    db.add(customer); db.commit()\n    yield customer\n    db.delete(customer); db.commit()",
      "purpose": "테스트 customer 생성 후 yield → 테스트 종료 시 정리",
      "extraction_method": "hybrid",
      "confidence": 0.85
    },
    {
      "pattern_kind": "wait-strategy",
      "framework": "playwright",
      "file": "tests/e2e/orders.spec.ts",
      "line_start": 60,
      "line_end": 65,
      "snippet": "await page.waitForResponse(r => r.url().includes('/api/orders') && r.status() === 201);",
      "purpose": "주문 생성 API 응답 대기 후 다음 단계",
      "extraction_method": "ast",
      "confidence": 1.0
    },
    {
      "pattern_kind": "page-object",
      "framework": "playwright",
      "file": "tests/e2e/pages/SignupPage.ts",
      "line_start": 1,
      "line_end": 50,
      "snippet": "export class SignupPage {\n  constructor(private page: Page) {}\n  async fillEmail(v: string) { await this.page.fill('[data-testid=email]', v); }\n  ...\n}",
      "purpose": "Signup 페이지 page-object pattern - 셀렉터 추상화",
      "uses_data_testid": ["email", "password", "name", "birth_date", "signup-submit"],
      "extraction_method": "hybrid",
      "confidence": 0.85
    }
  ]
}
```

### 4.3 패턴 분류 (LLM 분류 — confidence 0.85)
| pattern_kind | 의미 |
|---|---|
| `auth-setup` | 로그인 / 토큰 / 세션 셋업 |
| `db-seed` | DB 테스트 데이터 준비 |
| `wait-strategy` | 대기 패턴 (API 응답, element visible 등) |
| `page-object` | 페이지 단위 추상화 |
| `assertion` | 검증 패턴 (status code, response body, DB row 등) |
| `cleanup` | 테스트 종료 후 정리 |
| `fixture` | pytest/jest fixture |
| `mock` | API/DB mock |
| `unknown` | 위 분류 매칭 안 됨 (LLM 무판단 — HITL 검토 권장) |

### 4.4 활용
- TC 생성 시 `auth-setup` 패턴 → 시나리오의 precondition (로그인) 흐름 재사용
- `db-seed` 패턴 → TV 생성 시 같은 패턴으로 seed
- `wait-strategy` → UITestTool 의 wait 정책에 활용
- `page-object` → ActionMapper 의 selector 매핑 시 참조 (셀렉터 카탈로그 보강)

---

## 5. confidence 매트릭스 종합

| 영역 | 필드 | 추출 방법 | confidence |
|---|---|---|---|
| selectors | testid/html_type/v_model | AST | 1.0 |
| selectors | semantic_kind/semantic_purpose | LLM | 0.85 |
| selectors | dynamic.semantic_purpose | LLM 추론 | 0.65 |
| routes | path/component/params | AST | 1.0 |
| schemas | fields/validators | AST | 1.0 |
| schemas | response_schemas.status_codes (handler 추출) | AST | 1.0 |
| test_patterns | pattern_kind 분류 | LLM | 0.85 |
| test_patterns | snippet 그대로 보존 | AST | 1.0 |
| test_patterns | purpose 자연어 | LLM | 0.85 |

유빈 agent 는 `confidence >= 0.85` 만 신뢰. `confidence < 0.85` 는 HITL 검토 권장 표기.

---

## 6. 다음 단계

- PoC 2: AST 추출 PoC — Vue 의 `data-testid` 만 우선 (selectors 일부)
- PoC 3: S3 저장 + `metadata_writer.upsert_metadata_index(service_id, kind, sub_kind, payload)`
- PoC 4: 조회 헬퍼 `load_metadata_index(service_id, kind, sub_kind)`
- PoC 5: 다른 3 영역 확장
