# QApilot 아키텍처 다이어그램 설계 갭 분석

> **작성일**: 2026-05-19  
> **작성자**: A+D 담당 (kimjuhwan)  
> **관련 자료**: `qa_agent_architecture_0519.drawio.png`, `implementation-plan.md` (v1.8), `generated-code-architectural-purpose.md`

---

## 1. 개요
최근 공유된 QApilot 아키텍처 다이어그램(`qa_agent_architecture_0519.drawio.png`)과 실제 구현 명세서(`implementation-plan.md` v1.8)를 대조해 본 결과, 파이프라인의 데이터 흐름과 실행 구조에서 몇 가지 의미론적(Semantic) 차이가 발견되었다. 

프론트엔드 스택(Vue.js 표기)의 경우 추후 논의될 수 있으므로 본 문서에서는 제외하고, 파이프라인(Layer) 분리 구조와 UI Test Tool의 실행 주체에 초점을 맞춰 발견된 차이점을 기록한다.

---

## 2. 파이프라인(Layer) 분리 구조의 차이점

### 다이어그램 상의 표현
`Layer 1. 시나리오 생성` 이라는 하나의 영역 안에 "시나리오 생성 Agent"부터 "Playwright 코드 생성 Agent"까지 직렬로 연결된 형태로 묘사되어 있다.

### 실제 구현 (v1.8 기준)
코드베이스와 명세서 상의 파이프라인은 **Layer 1A (`generate_scenarios`)** 와 **Layer 1B (`generate_code`)** 로 완전히 분리되어 작동한다.
- 시나리오가 생성되면 파이프라인이 정지하고 디스크(`.qapilot/scenarios/`)에 저장된다.
- 이 분리된 틈을 이용해 **사용자가 대시보드에서 시나리오를 직접 검토하고 수정/삭제**하는 과정(HITL 대체)이 들어간다.
- 그 이후 별도의 명령(`generate code`)을 통해 Layer 1B가 가동되며 액션 매핑 및 코드 생성이 이루어지는 구조다.

---

## 3. UI Test Tool 실행 주체와 산출물의 괴리

### 다이어그램 상의 표현
다이어그램에서는 `Playwright 코드 생성 Agent`가 작성한 JS 코드가 `Layer 2`의 `UI 테스트 Tool`로 넘어가서 직접 실행되는 것처럼 시각화되어 있다.

### 실제 구현 (v1.8 기준)
**실제 테스트 실행(Layer 2) 단계에서는 생성된 JS 코드를 사용하지 않는다.**
- `UITestTool`은 파이썬 `async_playwright`를 사용하여, `ActionMapperAgent`가 출력한 `ActionMapping` (JSON 데이터)을 **직접 파싱하여 브라우저를 조작**한다.
- 여기에는 DOM을 찾지 못했을 때 selector_type을 바꿔가며 재시도하는 복잡한 **의미적 재시도(Fallback Chain)** 로직이 포함되어 있다.

### 💡 보충: JS 코드를 생성하는 이유
실행용 엔진이 JS 코드를 쓰지 않는데도 굳이 `CodeGeneratorAgent`가 JS 코드를 생성하는 이유는, 해당 파일이 파이프라인 내부 연료가 아니라 **외부로 제공되는 최종 산출물(Deliverable)** 이기 때문이다. 
- 비즈니스 의도가 담긴 주석과 테스트명으로 가독성을 높여 디버깅을 돕고,
- QApilot 사용을 중단하더라도 고객이 자체 CI/CD에 그대로 이식할 수 있도록(Ejection 보장) 하는 아키텍처적 의도가 담겨 있다.
*(상세 내용은 `generated-code-architectural-purpose.md` 참조)*

---

## 4. 요약
다이어그램은 프로젝트의 전체 비전과 흐름을 잘 보여주지만, 실제 엔지니어링 뎁스에서는 Layer의 물리적 분리(1A/1B)와 UI 테스트 실행 주체(JS 실행 vs Python JSON 직접 파싱) 부분에서 다른 구조를 취하고 있다. 개발 시 이 두 문서 간의 갭을 인지하고 진행할 필요가 있다.
