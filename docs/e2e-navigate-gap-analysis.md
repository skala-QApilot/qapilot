# QApilot e2e 100% UI Fail 의 구조적 원인 분석 — navigate step 부재의 사슬 작용

> **작성일**: 2026-05-19
> **작성자**: A+D 담당 (kimjuhwan)
> **기반 trace**: `e1796b43-eb9d-4da1-8dab-887f459ad07f` (통합 e2e — PR #106 + #110 + #114 + #115 + #120 누적 후)
> **관련 자료**: `docs/implementation-plan.md` v1.8.1, `docs/frontend-dom-scan-gap.md`, `docs/architecture-diagram-gap-analysis.md`

---

## 요약 (TL;DR)

1. PR #106/#110/#114/#115/#120 누적 머지 후 통합 e2e 결과: **PR #110 (CodeGen TC-별 분할) 효과 명백 검증** (.js 60건 생성), **PR #114/#115 (UITestTool 옵션 A chain + 옵션 B DOM scan) 효과는 적중 0회**.
2. 본인 PR 자체는 의도대로 100% 구현됨. PR #114/#115 미발현 원인은 **외부 조건**: ActionMapping 첫 step 이 navigate 가 아닌 DOM action (fill/click) 이라 SUT 의 잘못된 페이지 (`/plans` redirect 후) 에서 시도.
3. 근본 사슬: **ScenarioGen** (시나리오에 페이지 이동 명시 없음) → **ActionMapper** (navigate auto-prepend 부재) → **UITestTool** (target_url `/` 진입 후 vue-router redirect 인식 안 함).
4. 옵션 C (UITestTool auto-navigate, D 영역) 또는 B/C 영역의 근본 해결 후 PR #114/#115 효과가 비로소 측정 가능.

---

## 1. 실측 결과 (trace `e1796b43`)

### 1.1 단계별 시간/비용

| 단계 | 시각 | 소요 | 비용 |
|---|---|---|---|
| `generate code` | 05:22:33 → 05:31:06 | **8분 33초** | $0.0425 |
| `test` | 05:31:07 → 06:18:36 | **47분 30초** | $0.0647 |
| **합계** | | **약 56분** | **$0.1072** |

### 1.2 PR 별 효과

| PR | 결과 | 효과 |
|---|---|---|
| **#110** (CodeGen TC-별 분할) | `codegen_complete success=60 failed=13` | ✅ **명백 검증** — `.qapilot/generated-code/` 60건 생성 (이전 0 → +60), 13 TC skip graceful |
| **#106** (CodeGen graceful) | `code_generate_partial_failure` 1회 발화 | ✅ pipeline_complete 정상 — 부분 실패 가시화 |
| **#114** (옵션 A chain) | `ui_fallback_chain_retry` **286회 / success 0회** | ⚠️ chain 시도 활발 / 적중 0 |
| **#115** (옵션 B DOM scan) | `ui_fallback_dom_scan_*` **0회**, `dom_scan_evaluate_failed` (debug) **0회** | ⚠️ DOM scan 정상 호출 / fuzzy match 모두 None |
| **#120** (옵션 B 정정) | 예외 분류 일관성 + edge case 9건 테스트 | ✅ 동작 영향 없음 (정확성 강화) |

### 1.3 UI 결과 분포

| 항목 | 수치 |
|---|---|
| UI status | 73/73 **fail** (pass 0) |
| fast (<10s) | 0 |
| 10~25s | 6 |
| timeout (~30s) | 67 (92%) |
| UI 단계 총 시간 | **30.6분** (이전 e2e 20.2분 대비 +50% — chain timeout 분배 부작용) |

---

## 2. 근본 원인 — navigate step 부재의 사슬 작용

### 2.1 SUT vue-router 의 `/` 경로 동작

```js
// frontend/src/router/index.js
{ path: '/', redirect: () => (auth.token ? '/dashboard' : '/plans') }
```

→ 토큰 없이 `/` 진입 시 **`/plans` 로 redirect** (Login.vue 가 아닌 Plans.vue 렌더링).

### 2.2 UITestTool 의 진입 동작

- `target_url = http://localhost:3000/` (qapilot.config.yaml)
- `page.goto(target_url)` 호출 (`_test_execution` 노드)
- 결과: **`/plans` 페이지에서 첫 step 실행**

### 2.3 ActionMapping 의 첫 step (실측)

`.qapilot/action-mappings/TS-001-TC-01.json`:

```json
{
  "tc_id": "TS-001-TC-01",
  "steps": [
    {"step_no": 1, "action": "fill",   "selector": "이메일을 입력하세요", "selector_type": "placeholder", "value": "test@example.com"},
    {"step_no": 2, "action": "fill",   "selector": "비밀번호", "selector_type": "label", "value": "validPassword123"},
    {"step_no": 3, "action": "click",  "selector": "button:로그인", "selector_type": "role", "api_endpoint": "POST /login"},
    {"step_no": 4, "action": "assert", "selector": "JWT가 발급되고 응답에 고객 정보가 포함된다", "selector_type": "text"}
  ]
}
```

→ **navigate step 없음**. step 3 의 `api_endpoint="POST /login"` 가 힌트지만 어디서도 활용 안 됨.

### 2.4 사슬 결과

```
[1] target_url = "http://localhost:3000/"
    page.goto("/") → vue-router 가 /plans 로 redirect → Plans.vue 렌더링
              ↓
[2] step 1 (fill, selector="이메일을 입력하세요", selector_type="placeholder") 시도
    chain [placeholder → label → text] 각 step 모두 timeout
              ↓
[3] 옵션 B _fallback_dom_scan 호출
    page.evaluate("input/button/select/...") → Plans.vue 의 element 수집
    candidates = ["5G 데이터 무제한", "신청", "상세보기", "요금제 비교", ...]
    target = "이메일을 입력하세요"
    fuzzy match score 모두 0.6 미만 → return None
              ↓
[4] 마지막 chain timeout (PWTimeoutError) raise → TOOL_UI_LOCATOR_NOT_FOUND
```

### 2.5 Fuzzy match 시뮬레이션 — 결정 증거

**target**: `"이메일을 입력하세요"` (lower)

#### 실제 `/plans` 페이지 (e2e 가 시도한 잘못된 페이지)

| candidate | SequenceMatcher.ratio | 포함관계 보너스 | final | 임계값(0.6) |
|---|---|---|---|---|
| "5G 데이터 무제한" | 0.200 | +0.00 | 0.200 | ❌ 미달 |
| "LTE 베이직" | 0.118 | +0.00 | 0.118 | ❌ 미달 |
| "신청" | 0.000 | +0.00 | 0.000 | ❌ 미달 |
| "상세보기" | 0.143 | +0.00 | 0.143 | ❌ 미달 |
| "요금제 비교" | 0.125 | +0.00 | 0.125 | ❌ 미달 |

→ **모두 미달 → return None → 옵션 B 효과 0**

#### 이상적 `/login` 페이지 (navigate 가 선행됐다면)

| candidate (Login.vue 실측) | ratio | bonus | final | pass? |
|---|---|---|---|---|
| `<label>` "이메일" | 0.462 | +0.20 | **0.662** | ✅ **PASS** |
| `<label>` "비밀번호" | 0.000 | +0.00 | 0.000 | 미달 |
| `placeholder="example@email.com"` | 0.000 | +0.00 | 0.000 | 미달 |
| `placeholder="비밀번호 입력"` | 0.353 | +0.00 | 0.353 | 미달 |

→ **`get_by_label("이메일")` 로 적중 가능했을 것** — 옵션 B 의 fuzzy match 로직 자체는 정확.

**결론**: 옵션 A chain + 옵션 B DOM scan 두 메커니즘 모두 알고리즘 결함이 아니라 **잘못된 페이지에서 시도** 라는 외부 조건이 원인.

---

## 3. 책임 영역 분석

| 영역 | 책임 | 누락 |
|---|---|---|
| **B (ScenarioGen)** | 시나리오 비즈니스 의도 표현 | given/when/then 에 "/login 페이지로 이동" 같은 페이지 명시 없음. LLM 이 비즈니스 의도만 표현하고 위치 정보 누락 |
| **C (ActionMapper)** | ActionMapping 정규화·표준화 | navigate step auto-prepend 로직 없음. `api_endpoint` 힌트 (예: `POST /login`) 가 있어도 URL prefix 추출해 첫 step 으로 prepend 안 함 |
| **D (UITestTool, 본인)** | target_url 진입 + step 실행 + fallback | `target_url=/` 진입 후 vue-router redirect 인식 안 함. 첫 DOM action 의 page mismatch 사전 점검 없음 |

→ **3 영역 모두 navigate 책임 회피 — 사슬 끝까지 가서 100% fail**.

---

## 4. 본인 PR 의 의도 vs 실제 효과 (재정의)

| PR | 의도 | 구현 | 실제 효과 | 한계 원인 |
|---|---|---|---|---|
| **#110** | spec §4.5.1 LENIENT 본격 구현 (1 TC fail = 그 TC 만 skip) | ✅ 정확 (TC-별 LLM 호출 분할 + asyncio.gather + Semaphore 5) | ✅ **60 .js 생성, 13 skip 흡수** | — |
| **#106** | Agent fail 시 ActionMapping 보존 + pipeline 계속 | ✅ 정확 (노드 try/except) | ✅ pipeline_complete 정상 | — |
| **#114** | selector_type 별 entry 다양화로 적중률 ↑ | ✅ 정확 (text→label→placeholder→testid 등 chain) | ❌ 적중 0 | **올바른 페이지 가정** — selector 값 자체는 LLM 추측 그대로 유지, 페이지에 그 텍스트 없으면 4-entry 다 fail |
| **#115** | 실행 시점 DOM 정보 활용 fuzzy match | ✅ 정확 (page.evaluate + SequenceMatcher + 가산점 + 임계값) | ❌ 적중 0 | **올바른 페이지 가정** — Plans.vue 의 element 와 "이메일을 입력하세요" 의 fuzzy score 모두 0.6 미만 |
| **#120** | PR #115 정확성·일관성·테스트 강화 | ✅ 정확 (5건 정정 + 9 테스트 추가) | (동작 영향 없음) | — |

→ **본인 작업은 모두 의도대로 100% 구현됨**. 효과 미발현은 본인 영역 밖 (navigate 부재).

---

## 5. 해결 방향 (영역별 분리)

### 5.1 D 영역 (본인, 즉시 가능 — 옵션 C)

**UITestTool 의 auto-navigate 보강**:

```python
async def _ensure_target_page(self, page, action_mapping):
    """첫 step 이 DOM action 일 때 api_endpoint 기반 URL 추론 + navigate 자동 prepend."""
    first_step = action_mapping["steps"][0]
    if first_step["action"] in _DOM_ACTIONS:
        # api_endpoint 힌트로 frontend route 추론
        target_route = self._infer_frontend_route(action_mapping)
        if target_route:
            await page.goto(target_url.rstrip('/') + target_route)
```

추론 로직 후보:
- ActionMapping 의 어떤 step 의 `api_endpoint` 추출 → POST `/login` → frontend route `/login`
- 또는 시나리오의 affected_files (`backend/app/routers/auth.py`) → frontend page 매핑
- 또는 selector 자연어 휴리스틱 (`로그인` 단어 포함 시 `/login`)

### 5.2 C 영역 (타팀, 근본 해결)

**ActionMapper 의 navigate auto-prepend**:
- 첫 step 이 DOM action 일 때 `api_endpoint` 힌트 + ScenarioGen 시나리오 메타 분석
- frontend route 추론 후 `{step_no: 0, action: "navigate", value: "/login"}` prepend
- spec §4.5.1 의 "ActionMapper: 가능한 한 실패하지 않고 표준화" 원칙 확장

### 5.3 B 영역 (타팀, 근본 해결)

**ScenarioGen 의 페이지 명시**:
- given 절에 `"/{route} 페이지에서"` 명시 또는
- 시나리오 메타에 `start_page` 필드 추가 (예: `"start_page": "/login"`)
- 라우터 기반 ScenarioGen (PR #103) 의 router_hint 가 이미 frontend route 와 1:1 매칭 가능 — auth 라우터 → `/login`

### 5.4 우선순위

| 우선순위 | 영역 | 작업 | 효과 |
|---|---|---|---|
| 1 | D (본인) | 옵션 C — UITestTool auto-navigate | 즉시 효과, 옵션 A/B 가치 비로소 발현 |
| 2 | C (타팀 제보) | ActionMapper navigate auto-prepend | 근본 (ActionMapping 자체에 navigate 포함) |
| 3 | B (타팀 제보) | ScenarioGen start_page 명시 | 근본 (시나리오 의도에 위치 정보 포함) |

---

## 6. 부수 발견 (이번 e2e 에서 추가로 확인)

### 6.1 `code_context_not_found` 73회 — fix_recommender 가 generated-code 미참조

- `.qapilot/generated-code/` 에 60건 .js 있으나 fix_recommender 가 모두 `code_context_not_found` warning 발화
- 추정: fix_recommender 가 root_cause 의 `affected_file` (backend Python code) 만 조회하고 generated-code (.js) 디렉토리 미참조
- F 영역 (fix_recommender, 타팀) — 별도 이슈 권장

### 6.2 ActionMapping 73건 — ActionMapper 가 78 입력 중 5건 누락

- TS-007 family 라우터의 일부 TC 누락 (이전 e2e 의 TS-007-TC-13 누락 패턴과 유사)
- 추정: 단일 LLM 호출의 token truncate (이슈 #107 이 D 영역에서 해결한 동일 패턴이 C 영역에도 존재)
- C 영역 — TC-별 분할 권고 이슈 권장

### 6.3 chain 도입 후 UI 단계 시간 +50%

| 시기 | UI 단계 |
|---|---|
| 이전 e2e (chain 도입 전, `721a4e4f`) | 20.2분 |
| 이번 e2e (chain 도입 후) | **30.6분** |

- 적중 없을 때 각 TC 가 30s full timeout (1차 10s + 2차/3차/4차 5s × 3 + 옵션 B DOM scan 5s)
- 적중 시 시간 단축 효과 큼 — 단 본 e2e 에서는 적중 0 이라 시간만 손해
- chain 길이 / timeout 단축 검토 가치 (선택)

---

## 7. 결론

**본인 PR 모두 의도대로 100% 구현되었으나**, 옵션 A/B 의 효과 발현은 **외부 조건 (올바른 페이지에 있을 것)** 에 묶임. 본 e2e 는 그 외부 조건이 결여된 상태 (`/` 진입 후 `/plans` redirect) 에서 측정되어 적중 0회. fuzzy match 시뮬레이션으로 검증 시 `/login` 이었다면 `get_by_label("이메일")` 가 score 0.662 로 PASS 가능했을 것 — **알고리즘 결함이 아닌 사용 조건 결함**.

본 보고서의 사슬 분석은:
1. 옵션 C (UITestTool auto-navigate) 작업의 motivation 정량 근거
2. B/C 영역 근본 해결 이슈 제보 시 첨부 자료
3. 향후 e2e 측정 baseline (옵션 C 머지 후 동일 시나리오 재실행 시 UI pass 출현 기대)

이 세 측면에서 활용된다.
