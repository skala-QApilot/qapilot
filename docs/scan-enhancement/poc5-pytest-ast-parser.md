# PoC 5 — pytest AST 추출 (sut_tests.patterns)

> 본인 영역 (주환). sut_tests.patterns 영역의 첫 framework (pytest). PoC 2 의 vue_sfc_parser 와 동형 — 추출기는 framework 일반, mini-bss-lite 의 test 파일은 검증 fixture 로만 사용.

---

## 1. 한 줄 요약

| | |
|---|---|
| **목적** | pytest `def test_*` + `@pytest.fixture` 함수를 `TestPatternRecord` (Pydantic) 로 추출 |
| **도구** | `tree-sitter-python` (본인 의존성 이미 있음 — codebase_scanner_tool 에서 사용 중) |
| **분류** | AST 만 — fixture / auth-setup / mock / unknown (LLM 의미 분류는 후속 PoC) |
| **검증 fixture** | mini-bss-lite/backend/tests/ (상현 commit `3fc4330` 의 9 파일) |
| **상태** | ✅ commit (브랜치 `feat/me/scan-enhancement-foundation`) |

---

## 2. 일반화 원칙 (도메인 무관)

### 2.1 추출기는 framework 일반 — SUT 의 비즈니스 도메인과 무관

본인 PoC 2 (Vue SFC) 와 동형:

| 층위 | PoC 2 (frontend.selectors) | PoC 5 (sut_tests.patterns) |
|---|---|---|
| 추출기 자체 | Vue SFC AST 일반 | **pytest AST 일반** |
| 분류 카테고리 | input/button/output/dynamic (HTML 일반) | **fixture/auth-setup/mock/unknown (테스트 일반)** |
| 검증 fixture | mini-bss-lite Signup.vue | mini-bss-lite backend/tests/ |
| 다른 SUT 적용? | ✅ Vue 면 됨 | ✅ pytest 면 됨, 비즈니스 로직 무관 |

### 2.2 격차 A-2 방어 (BSS 한국어/도메인 키워드 hardcoded 금지)

❌ 금지:
```python
if "create_customer" in fixture_name: ...
if "/api/plans" in url: ...   # 도메인 종속
```

✅ 본 추출기:
```python
_AUTH_URL_KEYWORDS = ("/auth", "/login", "/signup", "/logout",
                       "/token", "/session", "/oauth", "/jwt")
# HTTP 인증 일반 URL — 비즈니스 도메인 무관
```

다른 SUT (kshyun Spring service 등) 의 pytest 테스트에도 그대로 동작.

---

## 3. 추출 정책 (AST 만 — confidence 1.0)

### 3.1 함수 수집 대상
- `def test_*` 또는 `async def test_*`
- `@pytest.fixture` 또는 `@pytest.fixture(...)` 데코레이터가 붙은 함수
- 그 외 일반 helper 함수 = 제외 (본 PoC 단계 — 후속에서 확장)

### 3.2 framework 감지 (`_detect_framework`)
다음 중 하나라도 있으면 pytest:
- `import pytest`
- `@pytest.fixture`
- `def test_` (모듈/줄 시작)
- `async def test_`

→ test_*.py 가 `import pytest` 없이 conftest fixture 자동 주입에 의존해도 잡힘 (mini-bss-lite/backend/tests/ 의 8 파일 패턴).

### 3.3 분류 (`_classify_test_body`)
| 조건 | pattern_kind |
|---|---|
| `@pytest.fixture` 데코레이터 있음 | **fixture** |
| body 에 `monkeypatch.setattr` / `mock.patch` / `MagicMock` | **mock** |
| body 에 `.get(...)` / `.post(...)` / `.put(...)` / `.patch(...)` / `.delete(...)` + URL 에 인증 키워드 | **auth-setup** |
| 그 외 | **unknown** (LLM 의미 분류 대기) |

`unknown` = **모호하다는 사실을 명확히 표현**. false positive 안 만듦 — 본인 영역 책임은 "확실한 것만 분류, 나머지는 LLM 보강에 위임".

---

## 4. 실 환경 검증 — 상현 commit 3fc4330 의 9 파일

```text
conftest.py      9 records  {'fixture': 9}
test_auth.py    13 records  {'auth-setup': 13}    ← URL 키워드 분류 100% 정확
test_billing.py  6 records  {'unknown': 6}        ← LLM 보강 대기
test_family.py  15 records  {'unknown': 15}       ← LLM 보강 대기
test_notices.py  3 records  {'unknown': 3}
test_orders.py  37 records  {'unknown': 36, 'mock': 1}   ← mock.patch 검출
test_plans.py    3 records  {'unknown': 3}
test_tier.py    16 records  {'unknown': 16}
test_usage.py    4 records  {'unknown': 4}
─────────────────────────────────────────────────────
전체           106 records  {auth-setup: 13, fixture: 9, mock: 1, unknown: 83}
```

분포 해석:
- **fixture 9** = conftest 의 9개 (engine, db_session, _mock_contracts, client, auth_headers, make_customer, customer, auth_client, plan_ids) 모두 잡음
- **auth-setup 13** = test_auth.py 의 13 test 모두 (URL `/signup`, `/login`, `/me` → auth 키워드 매칭)
- **mock 1** = test_orders.py 의 monkeypatch 사용 test
- **unknown 83** = billing/family/orders/plans/tier/usage 등 도메인 특화 test → LLM 보강에서 db-seed/cleanup/page-object 등 의미 분류

### 4.1 통합 end-to-end (PoC 3 + 4 와 결합)

```text
[PoC 5] extract_patterns_from_pytest_file (× 3 파일: auth/orders/conftest)
       → 59 records
       │
       ▼
[caller] SutTestsPatternsIndex(service_id, commit_sha, patterns=...)
       │
       ▼
[PoC 3] upsert_metadata_index — S3 PUT + DB INSERT (sub_kind="patterns")    ✅
       │
       ▼
[PoC 4] load_metadata_index(sid, "sut_tests", "patterns")
       → 59 records 그대로 조회                                              ✅
       │
       ▼
분포 일치: {fixture: 9, auth-setup: 13, unknown: 36, mock: 1}                ✅
```

PoC 3/4 가 sub_kind="patterns" 추가만으로 충돌 없이 동작 — DDL UNIQUE 제약 + Pydantic schema 의 일반화 설계가 정합.

---

## 5. 단위 테스트 (`tests/test_pytest_ast_parser.py` — 22/22 PASS)

| 그룹 | 케이스 |
|---|---|
| framework 감지 | import pytest, @pytest.fixture, def test_, async def test_, non-test → unknown |
| 분류 헬퍼 | auth-setup (signup/login), mock (monkeypatch), unknown (일반 GET) |
| 통합 추출 | count, fixture/auth-setup/mock/unknown 분류, framework=pytest, confidence=1.0/method=ast |
| 메타 | extracted_from relative path, line range 합리성, snippet = 함수 body 전체 |
| env_vars | os.getenv/os.environ 추출, 빈 결과 |
| 에러 핸들링 | 비-pytest 파일 = 빈 list, missing file = 빈 list (graceful) |

fixture: `tests/fixtures/pytest/sample_auth_test.py` — 2 fixture + 4 test (auth/non-auth/mock).

---

## 6. 격차 매핑 (PoC 0 의 8 격차)

| 격차 | 본 PoC 5 의 기여 |
|---|---|
| **A-1 오라클** | `TestPatternRecord.snippet` 으로 SUT 의 검증 인텐션 직접 LLM 에 노출 → 명세 vs 코드 동작 차이 발견 가능 |
| **A-2 도메인 hardcoded** | URL 키워드 = HTTP 인증 일반 (auth/login/signup/...). 비즈니스 도메인 (요금제/주문) 키워드 0 |
| **B-3 캐시** | PoC 3 의 `(commit_sha, kind, sub_kind)` cache 그대로 적용 |
| **B-4 결정성** | tree-sitter AST = 결정성 + sort_keys 직렬화 |

---

## 7. 다음

| PoC | 본 PoC 5 와의 관계 |
|---|---|
| 5.1 (LLM 의미 분류) | `unknown` 83개 → LLM 으로 `db-seed/cleanup/wait-strategy/page-object/assertion` 분류 + confidence 0.85 |
| 5.2 (Playwright extractor) | 같은 패턴 — `qapilot/scan/extractors/playwright_parser.py` + framework="playwright" |
| 5.3 (Cypress/Jest 등) | 같은 패턴 — framework 단위 분리 |
| 6 (TVValidator) | `load_metadata_index("sut_tests", "patterns")` 로 SUT 의 검증 패턴 참조 + TV 검증 oracle |
| 다른 영역 | routes (vue-router/react-router AST), schemas (Pydantic/SQLAlchemy AST) |
