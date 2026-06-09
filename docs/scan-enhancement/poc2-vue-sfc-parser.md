# PoC 2 — Vue SFC AST 추출 (frontend.selectors)

> 본인 영역 (주환). 2026-06-09 토론 결정 2 의 "AST 기본 + LLM 보강" 정책을 PoC 한 첫 추출기.

---

## 1. 한 줄 요약

| | |
|---|---|
| **목적** | `.vue` 파일의 `data-testid` element 를 카탈로그화 (input/button/output/dynamic) |
| **방법** | Node.js subprocess (`@vue/compiler-sfc`) 의 AST → Python wrapper 의 분류 + Pydantic 변환 |
| **결과** | mini-bss-lite Signup.vue: **9/9 testid = 100%** (이전 regex 휴리스틱 78%) |
| **상태** | ✅ commit (브랜치 `feat/me/scan-enhancement-foundation`), 단위 테스트 16/16 PASS |

---

## 2. Before / After

### 2.1 Before — `qapilot/tools/frontend_dom_scanner.py` (regex 휴리스틱)

```python
# 본인 기존 모듈 (PR #128)
# - tree-sitter-vue 가 Python 에 없어 regex 휴리스틱 채택
# - `<element ... data-testid="X" ...>` 패턴 정규식 + greedy quote 매칭
# - 정확도 측정: mini-bss-lite Signup.vue 9 testid 중 7 추출 (78%)
```

| testid | 추출? | 누락 원인 |
|---|---|---|
| name, email, password, birth_date | ✅ | — |
| signup-error, signup-success-toast, signup-submit | ✅ | — |
| **guardian-consent-field** | ❌ | `<div v-if="isMinor" data-testid="...">` 의 self-closing 검출 실패 |
| **guardian-consent** | ❌ | 부모 `v-if` 안의 input — 부모 트리 추적 불가 |

### 2.2 After — `qapilot/scan/extractors/vue_sfc_parser.py` (AST)

```text
.vue file
   │
   ▼  ① subprocess
qapilot/node-bridge/parse_vue_sfc.js    (Node.js + @vue/compiler-sfc)
   │
   ▼  ② JSON stdout (raw AST)
qapilot/scan/extractors/vue_sfc_parser.py
   │   ③ AST walk (재귀)
   │      - parent_v_if 추적 (DIRECTIVE name='if')
   │      - data-testid (ATTRIBUTE) 가진 ELEMENT 만 catalog
   │      - tag 별 분류 (input/textarea/select / button / 그 외)
   │      - parent_v_if 있으면 DynamicElement 로 재분류
   │
   ▼  ④ Pydantic instance
qapilot.shared.metadata_schemas:
   InputElement / ButtonElement / OutputElement / DynamicElement
```

| testid | 추출? | 분류 | v_if | line |
|---|---|---|---|---|
| name | ✅ | InputElement | — | 16 |
| email | ✅ | InputElement | — | 27 |
| password | ✅ | InputElement | — | 38 |
| birth_date | ✅ | InputElement | — | 50 |
| **guardian-consent-field** | ✅ | OutputElement | `isMinor` (자체 v-if) | 58 |
| **guardian-consent** | ✅ | **DynamicElement** | `isMinor` (부모 v-if) | 60 |
| signup-error | ✅ | OutputElement | `error` | 69 |
| signup-success-toast | ✅ | OutputElement | `success` | 70 |
| signup-submit | ✅ | ButtonElement | — | 73 |

**정확도: 9/9 = 100%.**

### 2.3 추가 속성 추출 (AST 만으로 가능한 정보)

| element | 추가 속성 |
|---|---|
| email (Input) | `v_model='form.email'`, `html_type='email'`, `required=True`, `validators=['required','email']`, `placeholder=None` |
| signup-submit (Button) | `html_type='submit'`, `disabled_when.expr='loading \|\| (isMinor && !form.guardian_consent)'` |

`disabled_when.semantic` 은 PoC 단계에서 `None` — LLM 의미 라벨링은 PoC 5+ 에서 보강 (confidence 0.85).

---

## 3. 안정성 점검 결과 (사용자 verbatim 조건 — "SaaS 구조에서 문제가 생기지 않는다면 진행")

본인이 진행 전 점검한 5 항목 + 결과:

| 우려 | 점검 결과 |
|---|---|
| Node.js 런타임 의존 | qapilot 에 Dockerfile 없음 (docker-compose 만). 시스템 Node v25.9.0 가용. frontend build 도 npm 의존 → Node 이미 dev 전제. 영향 0. |
| subprocess crash | `qapilot/cli/_ensure_browser.py` 에 이미 `subprocess.Popen` 패턴 적용. 본인 wrapper 도 동일 — `subprocess.run(..., timeout=10, shell=False, check=False)` + `VueSfcParseError` graceful |
| command injection | `shell=False` + 인자 `[str(_BRIDGE_SCRIPT), str(file_path.resolve())]` 만 (텍스트 interpolation X) |
| latency | cold start 측정 **70ms/file** (Signup.vue 실측, errors=0). mini-bss-lite 19 .vue → 순차 1.3s. service 등록 시 1회 + commit_sha cache hit 시 0회 |
| 결정성 | Vue 공식 AST = same input → same AST ✅ (regex 휴리스틱은 정규식 변경 시 결과 변동) |

후속 production 단계 검토 사항 (현재 PoC 범위 외):
- Docker image 화 시 Node.js base layer 추가 (~80MB)
- N=많을 시 persistent Node process (stdin/stdout JSON RPC) 로 cold start 절감

---

## 4. 연결부 (caller → extractor → schema → 저장)

```text
[caller: PoC 5+ frontend scanner]
   │ for vue_file in service_repo:
   │     elements = extract_selectors_from_vue(
   │         vue_file, repo_root=..., commit_sha=...)
   │
   ▼
[extractor: qapilot/scan/extractors/vue_sfc_parser.py]      ← 본 PoC 2 산출물
   │ Public API: extract_selectors_from_vue(...) -> list[ExtractedElement]
   │ Raises: VueSfcParseError (caller 가 graceful 결정)
   │
   ▼
[node-bridge: qapilot/node-bridge/parse_vue_sfc.js]         ← 본 PoC 2 산출물
   │ @vue/compiler-sfc.parse() → raw AST JSON
   │ exit codes: 0=ok, 2=usage, 3=missing dep, 4=parse fail
   │
   ▼
[schema: qapilot/shared/metadata_schemas.py]                ← PoC 1 산출물
   │ InputElement / ButtonElement / OutputElement / DynamicElement
   │ → RouteSelectors → FrontendSelectorsIndex
   │
   ▼ (PoC 3)
[S3 writer: upsert_metadata_index(FrontendSelectorsIndex)]
   │ services/{service_id}/metadata-index/{commit_sha}/frontend-selectors.json
   │ + metadata_indices DB record
   │
   ▼ (PoC 4)
[reader: load_metadata_index(service_id, "frontend", "selectors")]
   │ → ActionMapper / ScenarioGenerator / TVValidator 컨텍스트 주입
```

---

## 5. 회의 결정사항 → 본 PoC 매핑

| 결정사항 (2026-06-09 토론) | 본 PoC 2 에서 어떻게 구현했나 |
|---|---|
| 2. 메타데이터 4 영역 (selectors/routes/schemas/patterns) | `frontend.selectors` 1 영역 PoC. routes/schemas/patterns 은 PoC 5+ |
| AST 기본 (confidence 1.0) | 모든 추출 element 의 `confidence=1.0`, `extraction_method="ast"` |
| LLM 보강 (의미 라벨 0.85) | `OutputElement.semantic_kind`, `ButtonElement.disabled_when.semantic` 필드만 마련. 실제 라벨링은 PoC 5+ |
| 결정성 ((commit_sha, file_path) cache) | wrapper 는 결정적 — 동일 input → 동일 output. cache 적용은 PoC 3 writer 에서 (`head_object` skip 정책) |
| `extracted_from` (file/line/sha 추적) | 모든 record 에 `ExtractedFrom(file, line_start, line_end, commit_sha)` 채움 |

---

## 6. 검증 매트릭스

### 6.1 단위 테스트 (`tests/test_vue_sfc_parser.py`)

16 케이스 PASS:

| 그룹 | 케이스 |
|---|---|
| 카운트/분류 | testid 6개 모두 추출, tag→class 매핑, parent_v_if dynamic 승격, self_v_if 유지 |
| 속성 추출 | v_model, html_type+required+validators, placeholder, disabled_when.expr, button.html_type |
| 메타 | relative path, commit_sha 보존, line_range 합리성, AST confidence=1.0 |
| 에러 핸들링 | 파일 없음 → VueSfcParseError, 빈 template → 빈 list, repo_root 외부 → 절대 경로 fallback |

fixture: `tests/fixtures/vue/signup_minimal.vue` (6 testid + parent v-if + self v-if + disabled_when).

### 6.2 통합 검증 (mini-bss-lite Signup.vue)

```text
추출 element 수: 9 / 9 (100%)
누락: 없음
추가 (false positive): 없음
```

---

## 7. 후속 (PoC 3+ 로 이어짐)

| PoC | 본 PoC 2 와의 관계 |
|---|---|
| 3 (S3 writer) | `FrontendSelectorsIndex` 를 받아 `services/{sid}/metadata-index/{sha}/frontend-selectors.json` PUT + `metadata_indices` DB upsert |
| 4 (reader) | `load_metadata_index(sid, "frontend", "selectors")` → caller (ActionMapper) 에 주입 |
| 5 (React) | `qapilot/scan/extractors/react_jsx_parser.py` — `@babel/parser` 동일 subprocess 패턴. `node-bridge` 디렉토리 공유 |
| 5+ (LLM 보강) | `OutputElement.semantic_kind` (success_toast/error_toast/...) + `DisabledWhen.semantic` → 별도 LLM 호출 + confidence 0.85 |
| 6+ (다른 영역) | routes (`vue-router.createRouter` 추출), schemas (Pydantic/FastAPI 추출), patterns (Playwright/Pytest 추출) |
