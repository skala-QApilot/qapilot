# Frontend DOM 인덱싱 부재 — backend 중심 스캔의 의도된 단순화와 NPT 비전 갭

**작성일**: 2026-05-19  
**작성자**: A+D 담당 (kimjuhwan)  
**발견 trigger**: `qapilot test` e2e 첫 완주 (trace `721a4e4f`) 의 UI 100% fail 분석

## 1. 발견 요약

`qapilot test` 가 mini-bss-lite SUT 에서 끝까지 도달했으나 **77 TC 모두 UI fail**. 실패 원인의 90% 이상이 `TOOL_UI_LOCATOR_NOT_FOUND` (`get_by_text` 47회 / `get_by_label` 20회 / `get_by_placeholder` 6회). LLM 이 추론한 selector 텍스트가 실제 SUT 페이지의 텍스트와 mismatch.

### 결정적 대비

| LLM 추론 (Report 본문) | 실제 SUT (Login.vue) |
|---|---|
| `get_by_placeholder("이메일을 입력하세요")` | `placeholder="example@email.com"` |
| `get_by_text("...")` × 47 | 페이지에 해당 텍스트 없음 |
| (미시도) | `<label>이메일</label>` ✅ 정확 매칭 가능 |
| (미시도) | `data-testid="email"` ✅ 가장 안정적 |

→ SUT 가 의도적으로 `data-testid` 를 박아뒀음에도 LLM 이 이를 모름. 결과적으로 잘못된 placeholder 텍스트만 시도해서 100% timeout.

## 2. 근본 원인

```
CodebaseScannerTool (C 영역, FR-000) 의 스캔 범위가 backend 중심
       ↓
.vue 파일 0건 스캔 (Login.vue / Signup.vue / Dashboard.vue 등 미스캔)
spec §6.1 의 4파일 (endpoints/models/callgraph/manifest) 자체가 backend 인덱싱 모델
       ↓
frontend DOM 인덱스 (testid/label/placeholder/aria) 가 어디에도 존재하지 않음
       ↓
ActionMapper (C 영역, FR-004) LLM 호출 컨텍스트에 SUT DOM 정보 0
       ↓
LLM 이 비즈니스 의도만 받고 placeholder 텍스트 추측
       ↓
UITestTool (D 영역) 이 실제 SUT 페이지에서 그 추측 selector 시도 → 100% timeout
```

### 실측 검증 (mini-bss-lite manifest.json)

| 항목 | 수치 |
|---|---|
| 스캔 파일 총 71건 (backend 47 / frontend 6 / 기타 18) | **.vue 파일 0건** |
| `endpoints.json` | **backend 33건** (`/health`, `/login`, `/signup` 등 — frontend DOM 정보 0) |
| `frontend-dom.json` 같은 인덱스 | **spec §6.1 에 없음** (구조 자체 부재) |

## 3. 설계 의도 평가 — **의도된 단순화 + 발견되지 않은 갭**

### 3.1 의도된 단순화 (합리적 이점)

| 영역 | Backend | Frontend |
|---|---|---|
| Framework 다양성 | FastAPI/Django/Flask/Spring/Express — endpoint 추출 패턴 비교적 표준 (decorator + route path) | React/Vue/Angular/Svelte + JSX/TSX/Vue SFC — 마크업 추출 패턴 매우 다양 |
| Convention 표준 | `@router.post("/login")` 매우 표준 | `data-testid` / `data-test` / `data-qa` / `data-cy` 등 무표준 |
| 비즈니스 의도 매핑 | `POST /login` ↔ "로그인" 직결 | `<button>로그인</button>` → `onClick` → form.submit → API → 다단계 분리 |
| AST parser 비용 | tree-sitter 1-2개 (Python, JS) | tree-sitter 다수 + Vue SFC 등 framework 별 별도 |

→ **MVP 단계에서 backend 중심 시작은 합리적**. 코드베이스 보편성·구현 비용·비즈니스 의도 매핑 명확성 측면에서 최적.

### 3.2 발견되지 않은 갭 (NPT 비전 차원)

| 비전 / spec | 본인 e2e 실측 |
|---|---|
| **NPT (No People Testing)** — §1.1 "QA 실행 인력 60~80% 축소" | UI 100% fail — frontend DOM 인덱싱 없으면 NPT 불가능 |
| §4.5.1 ActionMapper "가능한 한 실패하지 않고 표준화 (정규화 계층)" | SUT DOM 정보 없이 표준화 = LLM 추측만 |
| §4.5.1 UITestTool "실제 유효성 판정자" | 판정만 하고 보정은 없음 (1-step fallback 만) |
| §6.1 디렉토리 구조 | `frontend-dom.json` 같은 인덱스 자리 자체 부재 |

→ spec 어디에도 "frontend 스캔 후속 필요" 또는 "ActionMapper 의 SUT DOM 무지 한계" 가 명시 안 됨. **갭이 인식되어 후속으로 미뤄둔 것이 아니라, 발견되지 않은 채로 누적**.

## 4. 보완 방향 — 영역별 분류

| 영역 | 작업 | 의미 | 작업량 |
|---|---|---|---|
| **C** (CodebaseScannerTool, FR-000) | `.vue`/`.tsx`/`.jsx` 스캔 추가 + `frontend-dom.json` 신규 인덱스 + spec §6.1 갱신 | **근본 해결** — backend 인덱스 모델을 frontend 로 확장 | 큼 (Framework 별 parser, convention 다양성 흡수) |
| **C** (ActionMapper, FR-004) | frontend-dom 인덱스를 LLM 호출 컨텍스트에 주입 | **근본 해결** — LLM 추측에서 인덱스 참조로 전환 | 중 (프롬프트 갱신 + context 주입) |
| **D** (UITestTool, FR-006) — 옵션 A | selector_type 별 의미적 retry chain (testid → label → placeholder → text) | **D 영역 우회** — spec §4.5.4 "1-step 한정" 갱신 동반. 즉시 적용 가능 | 중 (~120줄 + 단위 테스트) |
| **D** (UITestTool, FR-006) — 옵션 B | 런타임 DOM scan + fuzzy match | **D 영역 근본 보완** — spec §4.5.1 "UITestTool = 실제 유효성 판정자" 와 완벽 부합 (실행 시점에 DOM 활용) | 큼 (~200줄 + DOM scan 모듈) |

### 4.1 옵션 B 의 의미적 정당성

UITestTool 의 옵션 B (런타임 DOM scan) 는 **spec 변경 없이** §4.5.1 의 UITestTool 책임 정의 그대로 부합:

- SUT 가 기동된 **실행 시점** = DOM 접근 가능
- UITestTool 이 1차 시도 실패 시 DOM scan → 페이지의 실제 testid/label/placeholder 수집 → ActionMapper 가 추측한 selector 와 fuzzy match → 2차 시도
- spec §4.5.4 갱신 (1-step → 의미적 chain) 만 동반

C 영역 근본 해결 (CodebaseScannerTool frontend 스캔) 이 별도 진행되어도 D 영역 옵션 B 는 보완적으로 가치 유지 — 런타임 DOM 은 실제 페이지 상태 (예: SPA 의 동적 렌더, A/B 테스트 분기) 까지 반영.

## 5. 권장 진행 순서

### 본인 D 영역 (즉시 시작)

| Step | 작업 | spec 영향 | PR |
|---|---|---|---|
| 1 | 옵션 A — UITestTool retry chain (selector_type 별 의미적 chain) | §4.5.4 갱신 (1-step → 의미적 chain) | 별도 |
| 2 | 옵션 B — UITestTool 런타임 DOM scan + fuzzy match | §4.5.4 보강 (실행 시점 DOM scan 명시) | 별도 |

### 별도 영역 (이슈 제보 후 타팀 또는 회의 결정)

| Step | 작업 | 영역 | 의미 |
|---|---|---|---|
| 3 | CodebaseScannerTool 의 `.vue`/`.tsx`/`.jsx` 스캔 + `frontend-dom.json` 인덱스 신설 | C (타팀) | 근본 (spec §6.1 확장) |
| 4 | ActionMapper LLM 호출 컨텍스트에 frontend-dom 인덱스 주입 | C (타팀) | 근본 (LLM 추측 → 인덱스 참조) |

## 6. 의미적 결론

> **설계의 단순화 선택은 합리적이었음**. backend endpoint 중심 인덱싱은 framework 다양성 회피와 비즈니스 의도 매핑 명확성을 얻음. 그러나 **NPT 비전 차원에서 frontend DOM 정보 부재가 ActionMapper 의 LLM 추측 한계로 누적**되어, 본인이 발견한 UI 100% fail 이 그 갭의 첫 실측 증거. 옵션 A + B 가 spec 변경 최소화하면서 D 영역에서 핵심 가치를 회복하는 가장 자연스러운 방향. C 영역 근본 해결은 별도 트랙으로 진행 권장.

---

## 부록 — 관련 PR/이슈

- PR #102 (kshyun, E): Layer 3 본 구현 — DefectClassifier 노드 제거
- PR #103 (유빈, B): ScenarioGen 라우터 기반 재설계
- PR #106 (본인, A): `_code_generate` 노드 graceful 흡수
- PR #110 (본인, D): CodeGenerator TC-별 LLM 호출 분할
- **이슈 신규 (D, 옵션 A)**: UITestTool selector_type 별 의미적 retry chain
- **이슈 신규 (D, 옵션 B)**: UITestTool 런타임 DOM scan + fuzzy match
- **이슈 신규 (C 영역 제보)**: CodebaseScannerTool 의 frontend 스캔 + ActionMapper 인덱스 주입
