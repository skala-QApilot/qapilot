# 팀 FAQ — CodeGenerator Agent 와 UI 테스트 Tool 의 분리 설계

> **작성일**: 2026-05-20
> **작성자**: A+D 담당 (kimjuhwan)
> **계기**: `qa_agent_architecture_0519.drawio.png` 의 흐름과 실제 구현 v1.9 의 차이로 인한 정리 필요
> **관련 자료**: `docs/generated-code-architectural-purpose.md`, `docs/architecture-diagram-gap-analysis.md`, `docs/implementation-plan.md` v1.9 (§2.3, §4.5)

---

## 팀원 질문

> **"왜 이전 Agent 에서 이어지는 흐름에 자연스럽게 Playwright 코드 생성 Agent 가 생성한 코드를 실행하지 않나요? 왜 테스트 Tool 에서 독립적으로 Playwright 를 실행하나요?"**

`qa_agent_architecture_0519.drawio.png` 다이어그램상 **Playwright 코드 생성 Agent → 테스트 Tool (UI 테스트 Tool: Playwright)** 의 화살표 흐름이 자연스러워, 당연히 코드 생성 Agent 의 산출물 (.js) 을 UI 테스트 Tool 이 실행한다고 가정하기 쉽습니다. 그러나 실제 구현은 다릅니다.

---

## 1. 표현 정확화 — "독립적으로 Playwright 실행" 이 아닙니다

두 컴포넌트가 모두 **Playwright** 라는 같은 도구를 쓰지만, **언어와 실행 방식이 다릅니다**.

| 컴포넌트 | 사용하는 Playwright | 산출물 / 실행 방식 |
|---|---|---|
| **Playwright 코드 생성 Agent** (Layer 1B) | JS Playwright Test (`@playwright/test`) | **`.js` 파일 생성** — 셸에서 `npx playwright test` 로 실행 가능한 형태 |
| **UI 테스트 Tool** (Layer 2) | Python `playwright.async_api` | **ActionMapping JSON 을 받아 브라우저 직접 조작** — Python 코드 안에서 `await page.fill(...)` 호출 |

즉 *"테스트 Tool 이 코드 생성 Agent 의 산출물을 무시하고 따로 또 코드를 작성한다"* 가 아니라, **두 컴포넌트가 같은 입력 (ActionMapping JSON) 을 받아 각각 다른 목적의 출력을 만든다** 가 정확한 표현입니다.

```
                       ┌──────────────────────────────────────────────┐
                       │   ActionMapping JSON                         │
                       │   (ActionMapperAgent 의 산출물,                │
                       │    selector·action·value 의 구조화된 명령서)     │
                       └─────────────┬──────────────────┬─────────────┘
                                     │                  │
                       ┌─────────────▼──────┐  ┌────────▼─────────────────┐
                       │ CodeGenerator Agent│  │   UI 테스트 Tool           │
                       │ (LLM, JS 코드 생성)  │  │ (Python async_playwright) │
                       │                    │  │                          │
                       │ → .js 파일          │   │ → 브라우저 직접 조작        │
                       │   (외부 산출물)       │  │   (실행 + 결과 캡처)        │
                       └────────────────────┘  └──────────────────────────┘
```

---

## 2. 왜 이렇게 분리했나? — 3가지 본질적 이유

### 이유 ① 실행 엔진의 결정성 (가장 중요)

UI 테스트 실행 중에는 다음 보정이 **실시간으로** 필요합니다:

- **selector 적중 실패 시 의미적 chain 재시도** (text → label → placeholder → testid 순차)
- **런타임 DOM scan + fuzzy match** (실제 페이지의 element 와 LLM 추측 비교)
- **auto-navigate** (api_endpoint 힌트로 frontend route 추론)
- **에러 코드 분류** (`TOOL_UI_LOCATOR_NOT_FOUND` / `TOOL_UI_TIMEOUT` / `TOOL_UI_ASSERTION_FAIL` 등 7종)
- **step 단위 screenshot / console log / trace_id 캡처**

이 모든 보정이 **Python 단일 프로세스 안에서 결정적으로** 가능합니다.

만약 `npx playwright test` 같은 **JS subprocess** 로 실행하면:

- ❌ Python 프로세스의 fallback 로직과 차단됨 (stdout/stderr parsing 만 가능)
- ❌ LLM 이 만든 JS 코드의 syntax error 가능성 (런타임 의미 손실)
- ❌ trace_id 보존, step 단위 메타데이터 캡처 어려움
- ❌ 한 TC 가 다음 TC 에 영향 (브라우저 세션 격리 어려움)

→ **Playwright 의 강력한 보정 기능을 활용하려면 같은 언어 (Python) 안에서 실행해야 합니다**.

### 이유 ② JS 코드는 "내부 연료" 가 아닌 "최종 산출물"

이 부분이 핵심 설계 결정입니다. JS Playwright 코드의 정체:

- ❌ 파이프라인 중간 단계의 연료가 **아님**
- ✅ 외부로 배출되는 최종 deliverable (자산)

세 가지 가치:

#### (1) 가독성·디버깅 매개체

JSON ActionMapping 은 기계에는 완벽하지만 사람에게는 불친절합니다. 같은 의미를 JS 로 표현하면:

```javascript
// Given: 유효한 이메일, 비밀번호를 입력한 상태에서
// When: 회원가입 요청 시
// Then: 가입이 완료되고 '가입 완료' 메시지가 표시된다
test('TC-001 정상 회원가입', async ({ page }) => {
  await page.goto('/signup');
  await page.getByPlaceholder('Email').fill(process.env.E2E_USER_EMAIL);
  // ...
});
```

→ 원본 시나리오의 Given/When/Then 의도가 주석·테스트명으로 보존됩니다. 개발자가 디버깅 시 **이 `.js` 파일 하나만 받으면 `npx playwright test` 로 로컬에서 즉시 재현 가능**합니다. 개발팀 ↔ QA 간 커뮤니케이션 도구.

#### (2) Vendor Lock-in 방지 (Ejection 보장)

B2B 도입 시 고객이 가장 우려하는 것 = 도구 종속성.

- 고객이 QApilot 사용을 중단해도 → 누적된 수천 개의 `.qapilot/generated-code/*.js` 는 **순수 Playwright 표준 코드**
- 자체 CI/CD (GitHub Actions, Jenkins) 에 그대로 이식 가능
- *"도입 후에도 자유롭게 떠날 수 있다"* 는 **강력한 세일즈 포인트**

#### (3) NPT (No People Testing) 비전과의 정합

QApilot 의 비전은 *"사람 없이 테스트 자동 운영"*. 그러나 **결과물 (test code) 은 사람이 읽을 수 있어야** 합니다. 자동화는 내부, 산출물은 사람 친화적 — 이 분리가 핵심.

### 이유 ③ Spec §4.5.1 의 명시적 책임 계층화

`docs/implementation-plan.md` §4.5.1 에 명시:

| 컴포넌트 | 책임 |
|---|---|
| **ActionMapper** (LLM) | 비표준 → 표준화 (의미 추출) |
| **CodeGenerator** (LLM) | JS 코드 생성 (외부 deliverable) |
| **UITestTool** (Tool, LLM 미사용) | **실제 유효성 판정자** (결정적 실행) |

즉 spec 차원에서 **LLM 은 의미 추출에만 사용, 실행은 결정적 Tool 이 담당** 으로 책임 분리되어 있습니다.

---

## 3. 다이어그램의 직관 vs 실제 (왜 오해 발생?)

다이어그램 (`qa_agent_architecture_0519.drawio.png`) 은 **제품 비전과 흐름**을 잘 보여주지만, 엔지니어링 디테일에서 두 가지 갭이 존재합니다:

| 다이어그램 표현 | 실제 구현 (v1.9) |
|---|---|
| 코드 생성 Agent → UI 테스트 Tool 의 직렬 화살표 | 두 컴포넌트가 **각각 ActionMapping 을 받음** (직접 코드 전달 X) |
| Layer 1 안에 코드 생성까지 포함된 것처럼 | **Layer 1A (`generate_scenarios`)** 와 **Layer 1B (`generate_code`)** 는 **물리적으로 분리된 파이프라인** — 사용자가 두 명령 사이에 시나리오 검토 가능 |

→ 다이어그램은 **제품 비전** 표현용. 코드 구현 디테일은 **`docs/implementation-plan.md` v1.9** 가 정답.

본 갭의 상세 분석은 `docs/architecture-diagram-gap-analysis.md` 참고.

---

## 4. 비유로 정리

> 자동차 정비공 한 명이 두 가지 일을 합니다.
>
> - **수리 보고서 작성** (JS 코드 = 고객용 산출물)
> - **실제 수리 작업** (UI 테스트 Tool 의 직접 실행)
>
> 보고서를 먼저 쓰고 그 보고서대로 수리하는 게 아니라, 같은 진단 정보 (ActionMapping) 를 갖고 **두 작업이 병렬로 수행**됩니다. 보고서는 고객 (개발자) 이 가져가서 다른 정비소에서도 활용 가능한 표준 양식이고, 수리 작업은 정비공이 자기 도구 (Python) 로 직접 합니다.

---

## 5. 참고 자료

팀 공유용 상세 문서:

- **`docs/generated-code-architectural-purpose.md`** — JS 코드의 존재 의의 (본 FAQ 의 원본)
- **`docs/architecture-diagram-gap-analysis.md`** — 다이어그램 ↔ 구현 갭 분석
- **`docs/implementation-plan.md`** v1.9 — §2.3 (모듈 호출 규칙), §4.5 (C/D 정책)

관련 e2e 분석 (이 분리 설계의 실측 검증):

- **`docs/e2e-navigate-gap-analysis.md`** — UI 테스트 Tool 의 옵션 A/B/C 결정적 보정 사례
- **`docs/frontend-dom-scan-gap.md`** — 인덱싱 시점 vs 실행 시점의 보완 관계

---

## 6. 본인 의견 — 다이어그램 갱신 권고

팀원 오해를 줄이려면 drawio 다이어그램에 다음 두 가지 시각적 표현 추가를 권합니다:

1. **두 갈래 화살표** — ActionMapping JSON 이 CodeGenerator 와 UITestTool 양쪽으로 분기
2. **JS 코드를 "외부 산출물 (Deliverable)" 박스** 로 별도 표기 (UITestTool 박스 안이 아닌)

---

## 7. 한 줄 요약

> **JS Playwright 코드와 UI 테스트 Tool 은 같은 ActionMapping 을 입력으로 받지만, 전자는 외부 고객용 산출물 (가독성·이식성), 후자는 내부 실행 엔진 (결정적 보정) 으로 의도적으로 분리되어 있습니다. spec §4.5.1 의 책임 계층화 — "LLM 은 의미 추출, Tool 은 실행" — 원칙의 구체 구현입니다.**
