# 생성된 JS 코드의 아키텍처적 존재 의의

> **문서 목적**: QApilot 파이프라인에서 실행(execution) 방식과 산출물(deliverable) 분리 설계에 대한 철학적, 기술적 배경 설명  
> **대상**: 팀원 전체 (특히 아키텍처 및 D 영역 담당자)  
> **최종 수정일**: 2026-05-19  
> **작성자**: A+D 담당 (kimjuhwan)

---

## 1. 개요 및 배경 의문

QApilot 아키텍처 (v1.8 기준), 실제 통합 테스트(Layer 2)를 수행하는 `UITestTool`은 `CodeGeneratorAgent`가 생성한 JavaScript 코드를 셸(shell) 레벨에서 실행(`npx playwright test`)하지 않습니다. 

대신, 이전 단계인 `ActionMapperAgent`가 출력한 `ActionMapping` (JSON 형태의 DOM 조작 시퀀스) 데이터를 Python `async_playwright`를 통해 **직접 해석하고 실행**합니다.

이러한 구조를 보면 필연적으로 다음과 같은 의문이 제기될 수 있습니다.
> *"어차피 테스트 실행 엔진(`UITestTool`)이 JS 코드를 안 쓴다면, 왜 비싼 LLM 토큰을 소모해가며 굳이 `CodeGeneratorAgent`를 거쳐 Playwright JS 코드를 생성하는가? 이는 불필요한 작업(Overhead)이 아닌가?"*

본 문서는 이 설계 결정에 담긴 깊은 의미론적(Semantic), 비즈니스적 의도를 3가지 관점에서 상세히 설명합니다.

---

## 2. 존재 의의 및 설계 철학

### 2.1. "실행용 엔진"과 "최종 산출물(Deliverable)"의 분리

NPT(No People Testing) 비전을 달성하기 위해, 내부 파이프라인은 기계(Machine)가 통제하기 가장 완벽한 형태를 취해야 합니다.

- **실행용 엔진 (Python + JSON)**
  - JSON 형식의 `ActionMapping`은 구조화된 데이터이므로 파이썬 코드(`UITestTool`)에서 파싱하고 제어하기가 매우 용이합니다.
  - 특히, DOM 요소를 찾지 못했을 때 즉시 다른 selector로 우회하는 **의미적 재시도(Fallback Chain)**, 실패 시 `TOOL_UI_LOCATOR_NOT_FOUND`와 같은 **정교한 에러 코드 추적** 등을 제어 흐름 안에서 구현하려면 Python 엔진이 직접 브라우저 세션을 쥐고 통제하는 것이 압도적으로 유리합니다.

- **최종 산출물 (Playwright JS Code)**
  - 반면, 시스템 밖으로 배출되는 최종 결과물은 단순한 중간 데이터(JSON)가 아니라 **완벽한 형태의 코드 자산(Asset)** 이어야 합니다.
  - `generate_code` 단계에서 생성되는 `.js` 파일은 QApilot이 인간을 대신해 작성해 낸 **'지적 재산'** 이자 **'최종 QA 보고서'** 의 성격을 가집니다.

### 2.2. 가독성 (Human Readable) 및 디버깅 매개체

JSON 데이터는 기계가 읽기엔 완벽하지만, 인간이 읽고 그 비즈니스적 맥락을 유추하기엔 매우 불친절합니다. `CodeGeneratorAgent`는 이를 보완하여 아래와 같은 형태의 코드를 생성해 냅니다.

```javascript
const { test, expect } = require('@playwright/test');

// Given: 유효한 이메일, 이름, 비밀번호를 입력한 상태에서
// When: 회원가입 요청을 전송하면
// Then: 회원가입이 성공하고 '가입 완료' 메시지가 반환된다
test('TC-001 정상 회원가입', async ({ page }) => {
  await page.goto('/signup');
  await page.getByPlaceholder('Email').fill(process.env.E2E_USER_EMAIL);
  // ...
});
```

- **맥락 보존**: 위 코드처럼 원래 `TestScenario`에 담겨 있던 기획 의도(Given/When/Then, 테스트명)를 주석과 함수명으로 살려냅니다.
- **디버깅 편의성**: QApilot이 찾아낸 버그를 개발자가 수정하려고 할 때, 이 JS 파일 하나만 로컬로 가져가면 `npx playwright test` 한 줄로 쉽게 문제를 재현하고 디버깅할 수 있습니다. 이는 개발팀과 QA팀 간의 완벽한 커뮤니케이션 도구가 됩니다.

### 2.3. Vendor Lock-in 방지 및 Ejection (독립) 보장

B2B 소프트웨어 관점에서, 특정 툴에 대한 종속성(Vendor Lock-in)은 고객사 입장에서 매우 큰 도입 리스크입니다.

- 만약 고객사가 향후 QApilot 솔루션 사용을 중단하더라도, 그동안 `.qapilot/generated-code/*.js` 형태로 누적된 수천 개의 테스트 스크립트는 전혀 무용지물이 되지 않습니다.
- 생성된 코드는 업계 표준인 순수 Playwright 프레임워크 기반이므로, 고객은 이 코드들을 자신들의 기존 CI/CD 파이프라인(GitHub Actions, Jenkins 등)에 그대로 이식하여 사용할 수 있습니다.
- 이는 QApilot 제품 도입에 대한 심리적 장벽을 낮추는 **강력한 세일즈 포인트**로 작용합니다.

---

## 3. 결론

`CodeGeneratorAgent`가 생성하는 Playwright JS 코드는 파이프라인 진행을 위해 잠시 쓰이고 버려지는 '중간 연료'가 아닙니다. 

이 코드는 **NPT 비전의 자동화를 수행하면서도 시스템을 블랙박스로 만들지 않고, 인간과 기계를 이어주는 소통의 매개체이자, 최종적으로 고객에게 귀속되는 독립적인 소프트웨어 자산**으로서 아키텍처 설계상 필수 불가결한 핵심 요소입니다.
