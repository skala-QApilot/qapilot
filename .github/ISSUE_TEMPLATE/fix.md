---
name: 버그 수정
about: 버그 및 오류 수정
labels: fix
---

## 문제 상황
ActionMapper가 UI selector를 생성할 때 `frontend.json`을 실질적인 후보 소스로 사용하지 않고 LLM이 selector를 자유 생성하고 있어, 실제 DOM에 없는 selector가 대량 발생한다. 특히 `frontend.json`은 legacy `init/rescan` 경로에서만 디스크 생성되고 있으며, 현재 PostgreSQL/S3 기반 codebase index mirror에는 `frontend` kind가 포함되지 않아 실행 시점에 일관된 frontend index를 읽지 못한다.

그 결과 다음 문제가 연쇄적으로 발생한다.

- `fill/click/assert` step에서 실제 화면과 다른 selector 생성
- `회원가입`, `click`, `123`, `valid_token` 같은 시나리오 문장/파라미터 값이 selector로 오염
- `selector_type: text` 비중이 과도하게 높아 strict mode / locator timeout / assertion fail 증가
- UITestTool의 런타임 보정이 최후 fallback 역할은 하지만, ActionMapping 생성 품질 문제를 근본적으로 막지는 못함

## 재현 방법
1. 
`system-under-test` 프로젝트에서 `qapilot test` 또는 SaaS 경로의 test-run을 실행한다.
2. 
trace 결과의 `action-mappings/*.json`과 `results/*/ui_result.json`을 확인한다.
3. 
다음과 같은 mismatch를 확인한다.

- 실제 Signup 화면 버튼 텍스트/식별자: `가입하기`, `data-testid="signup-submit"`
- 생성된 selector: `getByText("회원가입")`
- 실제 입력 label: `이름`
- 생성된 selector: `getByLabel("사용자 이름")`
- 실제 placeholder: `example@email.com`
- 생성된 selector: `getByPlaceholder("이메일을 입력하세요")`

## 기대 동작
ActionMapper는 selector를 자유 생성하지 않고, 실행 대상 서비스의 최신 `frontend.json`에서 추출한 후보만 사용해야 한다.

- `frontend.json`은 다른 codebase index와 동일하게 DB/S3에 저장된다.
- 실행 시 ActionMapper는 해당 서비스/커밋의 `frontend` index를 로드한다.
- selector는 후보 선택 방식으로 결정된다.
- `fill/click/assert` step은 `testid > role/name > label > placeholder > text` 우선순위로 안정적인 locator를 선택한다.
- UITestTool은 선택된 selector가 실패한 경우에만 locator chain/DOM scan/page-wide assert fallback을 수행한다.

## 실제 동작
현재는 다음처럼 동작한다.

- `frontend.json`은 `.qapilot/codebase-index/frontend.json`에만 생성되고, 실행별 `qapilot_dir` 경로 및 DB/S3 mirror에는 포함되지 않는다.
- ActionMapper는 `frontend_dom`을 참고 텍스트로만 프롬프트에 넣고, selector를 LLM이 직접 생성한다.
- 후처리는 assert 계열 일부만 적용되고 일반 `fill/click` selector 환각은 그대로 남는다.
- UITestTool은 다양한 locator 전략을 지원하지만, ActionMapping에서 이미 `selector_type: text`가 많이 생성되어 `get_by_text` 경로로 과도하게 몰린다.

실제 영향 예시:

- `TOOL_UI_LOCATOR_NOT_FOUND`
- `TOOL_UI_ASSERTION_FAIL`
- `TOOL_UI_UNKNOWN`
- 회원가입/가족/공지사항/결제수단/계약관리 시나리오에서 대량 fail

## 관련 파일 / 모듈
- `qapilot/qapilot/tools/frontend_dom_scanner.py`
- `qapilot/qapilot/orchestrator/pipeline.py`
- `qapilot/qapilot/db/code_writer.py`
- `qapilot/qapilot/agents/action_mapper_agent.py`
- `qapilot/prompts/action_mapper/system.md`
- `qapilot/qapilot/tools/ui_test_tool.py`

## 원인 추정
원인은 크게 세 가지로 추정된다.

1. `frontend.json` mirror 누락
- codebase index dual-write 대상에 `frontend.json`이 빠져 있어 DB/S3에서 일관되게 읽을 수 없다.

2. ActionMapper의 selector 자유 생성 구조
- `frontend.json`을 후보 집합이 아니라 단순 참고 컨텍스트로만 사용한다.
- LLM이 실제 DOM에 없는 문구/값을 selector로 생성한다.

3. 런타임 fallback 의존 구조
- UITestTool이 보정은 가능하지만, 정적 selector 품질이 낮아 fallback 비율과 fail 비율이 높아진다.

해결 방향:

- `frontend.json`을 `kind="frontend"`로 codebase index mirror에 포함
- 실행 시 S3/DB에서 `frontend` index를 로드해 ActionMapper context에 주입
- ActionMapper를 자유 생성에서 후보 선택 구조로 변경
- UITestTool은 마지막 런타임 보정 전용으로 유지
