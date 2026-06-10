# TV 단계 코드베이스 연결 — 사람을 위한 안내서

> 본 작업이 무엇을 하는가, 왜 하는가, 어떻게 연결하는가, 그래서 기존 흐름이 어떻게 바뀌는가 정리.

---

## 한 줄 요약

지금까지는 시나리오와 테스트 케이스를 **PRD 와 문서만 보고** 만들었다. 이 작업으로 **테스트 케이스에 들어갈 실제 값(TV)을 코드베이스와 DB 까지 본 다음** 채우게 된다.

---

## 지금 어디까지 와 있나

지금까지 두 사람이 한 일이 이렇게 있다.

### 유빈 이 만든 것
- **TS 생성**: PRD 요구사항만 보고 테스트 시나리오 목록을 만든다.
- **TC 생성**: TS 별로 문서 검색(Qdrant)을 돌려서 관련 문서를 찾고, 그 문서 안 내용으로 TC(given/when/then)를 만든다. TC 안에 `values` 칸이 있긴 한데 값이 문서에 없으면 `{설명}` 같은 빈 자리(placeholder)로 둔다.

### 주환 이 만든 것 (이미 develop 에 있음)
- **코드베이스 스캔**: 서비스의 코드를 다 읽어서 4가지로 정리해둔다.
  - 프론트의 버튼/입력창 이름표 (selectors)
  - 라우터(어떤 URL 이 어디로 가는지)
  - 백엔드 스키마(API 가 어떤 필드를 받는지)
  - SUT 의 기존 테스트 코드 패턴
- 이 4가지가 S3 와 DB 에 저장되어 있다.
- **검증 도구(TVValidator)** 와 **DB 스냅샷 도구(get_db_snapshot_cached)** 도 만들어 두었다.

---

## 무엇이 빠져 있나

회의에서 결정한 흐름:

```
1. PRD     → TS 생성
2. TS HITL 검증 (후순위)
3. TS + 문서 → TC (given/when/then)
4. TC + 코드베이스/메타데이터(sut test code, 라우터, 셀렉터, 스키마)/DB → TV (실제 값 채우기)
```

지금까지 1, 3 까지만 만들어졌다. **4 번이 통째로 빠져 있다.** 본 작업이 그 4 번을 채운다.

---

## 본 작업이 무엇을 하는가

새로운 단계를 하나 추가한다:

**TC 생성이 끝난 직후에, "TV 채우기" 단계를 끼워 넣는다.**

```
기존:  TS 생성 → TC 생성 (+ 빈 values) → 저장 → 끝
변경:  TS 생성 → TC 생성 (+ 빈 values) → TV 채우기 ★ 신규 ★ → 저장 → 끝
```

`TV 채우기` 단계가 하는 일:
1. TC 를 하나씩 가져온다.
2. TC 의 `api` 필드 (예: `POST /api/auth/signup`) 를 보고 **그 API 만 관련 있는** 스키마와 셀렉터를 메타데이터에서 골라낸다.
  - "관련있음": RAG X, 키워드 X, `load_metadata_index` 에서 정확한 키 찾음.
3. TC 의 `req_id` 를 보고 **그 요구사항만 관련 있는** 테스트 패턴을 골라낸다.
4. DB 에 가서 **실제 데이터를 일부 가져온다** (예: 이미 가입된 이메일 1개).
5. 골라낸 정보를 LLM 에 주면서 "이 정보로 TC.values 의 빈 자리를 채워라" 라고 시킨다.
6. LLM 응답을 받아서 **TVValidator 로 검증한다** — 스키마 맞나? 형식 맞나? DB 와 일관성 있나?
7. 검증 실패하면 LLM 에 "이렇게 틀렸으니 다시" 라고 알려주고 한두 번 재시도한다.
8. 최종 valid 한 values 를 TC 에 박는다.

---

## 왜 이렇게 하는가 — 가장 큰 결정 두 가지

### 결정 1: TV 생성을 TC 생성에서 분리한다

처음엔 "TC 만들 때 같이 codebase 도 한꺼번에 LLM 에 던지면 안 되나?" 라는 안도 있었는데, **정보가 너무 많아진다.**

mini-bss-lite 만 봐도 코드베이스가 selectors 125 + routes 16 + schemas 51 + patterns 등 합치면 수만 자가 된다. 이걸 통째로 LLM 한 번 호출에 다 넣으면 — LLM 이 정작 필요한 정보를 못 찾는다. (긴 prompt 의 중간 정보가 무시되는 알려진 현상)

유빈 이 한 것처럼 Qdrant 로 의미 검색해서 5개 chunk 만 뽑아 LLM 에 주는 게 깔끔하다. 그래서 **TC 단계는 유빈이 만든 대로 두고**, TC 생성 후에 별도로 TV 단계에서 **TC 의 api/req_id 를 기준으로 관련 있는 부분만 골라낸 다음** LLM 에 준다.

### 결정 2: DB 데이터를 일부만 LLM 에 노출한다 + 민감 정보 처리

회의에서 결정한 "DB 스캔 툴로 value 가져오기" 의 진짜 의미는 두 가지다.
- (가) DB 와 일관성 있는지 검증한다 — 이건 TVValidator 로 한다.
- (나) DB 에 있는 실제 값을 LLM 한테 보여줘서 활용하게 한다 — 예를 들어 "이미 가입된 이메일로 가입 시도" 같은 TC 에는 진짜 DB 에 있는 이메일 1개를 보여준다.

이 (나) 가 위험할 수 있다. password 나 token 같은 게 LLM 에 그대로 넘어가면 안 된다.

**중요 — 이전 PR #256 의 본질 재발 방지**:
이전에 CodeGenerator 가 password 같은 민감 값을 `process.env.E2E_USER_PASSWORD` 로 마스킹해서 LLM 에 보내면, LLM 이 그 마스킹된 형태를 응답에 그대로 박아넣어서 실제 테스트 실행 시 빈 string 으로 처리됐다 (HTML5 required validation fail → POST 0건). 본인이 PR #256 으로 ActionMapping 의 원본 value fallback 으로 fix.

본 작업도 같은 위험 — "마스킹된 값을 LLM 에 보이는" 방식은 같은 문제 재발. 따라서:

- **sensitive 필드는 LLM 컨텍스트에서 완전 제외**한다 (마스킹된 형태 노출 자체를 안 한다).
- TC.values 의 sensitive 필드는 **LLM 이 생성하지 않고** TV agent 가 placeholder (`process.env.TEST_PASSWORD`) 로 저장한다.
- 액션매핑 단계의 PR #256 의 ActionMapping 원본 value fallback 으로 실행 시점에 실제 값이 들어간다.

`SchemaField.sensitive=True` 표시 (`backend_schema_parser` 가 `password*` 컬럼에 자동으로 붙임) 를 이용해서 LLM 호출 직전에 sensitive 필드를 컨텍스트에서 제거한다.

---

## 그래서 흐름이 어떻게 보이게 되나

### 사용자 입장에서
바뀌는 거 거의 없다. API 한 번 호출하면 시나리오 + 테스트 케이스 + 값 까지 한 번에 만들어져서 떨어진다. 단 응답 시간이 조금 더 늘어난다 (LLM 호출이 한 번 더 들어가니까).

### 내부 흐름 (실험 모드 — prd_only_experiment)

```
이전:
  doc_import → requirement_extract
            → ts_generate_prd_only          (TS 만들기)
            → tc_generate_doc_search        (TC 만들기, values 는 placeholder)
            → save_experiment_scenarios     (저장)

변경:
  doc_import → requirement_extract
            → ts_generate_prd_only
            → tc_generate_doc_search
            → tv_generate_codebase_aware    ★ 새로 추가 ★
            → save_experiment_scenarios
```

### 데이터 형식 변화

TC 가 만들어진 직후 모습:
```json
{
  "name": "기본 회원가입",
  "given": "신규 사용자가 가입 페이지에 있다",
  "when": "유효한 정보로 가입을 요청한다",
  "then": "201 Created 응답을 받는다",
  "values": [
    {"field": "email", "value": "{유효한 이메일}", "type": "string"},   ← placeholder
    {"field": "password", "value": "{8자 이상 비밀번호}", "type": "string"}
  ]
}
```

TV 단계가 끝난 후 모습:
```json
{
  "name": "기본 회원가입",
  "given": "신규 사용자가 가입 페이지에 있다",
  "when": "유효한 정보로 가입을 요청한다",
  "then": "201 Created 응답을 받는다",
  "values": [
    {"field": "email", "value": "newuser_2026@test.com", "type": "string",
     "source": "llm+validated", "confidence": 1.0},          ← 실제 값 + 검증됨
    {"field": "password", "value": "TestPw1234!", "type": "string",
     "source": "llm+validated", "confidence": 1.0}
  ]
}
```

---

## 기존 e2e 흐름이 망가지지는 않나

본 작업의 핵심 원칙: **기존 흐름 100% 유지.**

- **일반 generate_scenarios 흐름** (코드베이스 스캔 → 시나리오 → 액션매핑) → 손대지 않는다.
- 본 작업의 변경은 오직 **실험 모드(prd_only_experiment)** 에만 영향. 일반 모드의 코드는 한 줄도 안 바꾼다.
- 액션 매핑 단계는 그대로. 액션 매핑이 받는 TC 형식은 동일 (`name/given/when/then/values/tags/...`). 다만 `values` 가 placeholder → 실제 값으로 채워진다.

기존 액션 매핑이 TC 를 보고 "이 값 가지고 이렇게 액션 매핑하라" 라고 하던 것이, 이제 더 정확한 값을 받게 되니까 **결과가 더 좋아진다**.

---

## 본 작업 후 새로 생기는 / 손대는 파일

### 새로 추가 (이 작업으로 만들 것)
- `qapilot/agents/tv_codebase_aware_agent.py` — TV 채우기 agent
- `qapilot/shared/metadata_filters.py` — TC 의 api/req_id 로 메타데이터 필터링 함수
- `qapilot/shared/sensitive_mask.py` — 민감 정보 마스킹 함수
- `prompts/tv_codebase_aware/{system,template}.md` — LLM 프롬프트
- `docs/tv-codebase-aware/security-considerations.md` — 보안 고려사항 (Q2)
- 단위 테스트 일체

### 살짝 수정
- `qapilot/orchestrator/pipeline.py` — `prd_only_experiment` 그래프에 노드 1개 + `_tv_generate_codebase_aware` 함수 추가
- `qapilot/orchestrator/state.py` — `tc_by_ts_index` 가 TV 단계에서 갱신되는 식으로

### 변경 0
- 일반 `generate_scenarios` 경로의 모든 코드
- `_codebase_scan` (1021ba9 의 `scan_all_metadata` 호출 그대로)
- ActionMapper, CodeGenerator, UITestTool 등 액션 매핑 이후 흐름

---

## 다음 단계

1. 이 README 의 방향이 맞는지 확인 후
2. `tv_codebase_aware_agent` 와 필터링 함수 / 마스킹 함수 작성
3. `prd_only_experiment` 그래프에 노드 연결
4. 보안 docs 작성 (Q2)
5. 단위 테스트 + 실 환경 e2e 검증 (mini-bss-lite 전체 흐름)

기존 일반 `generate_scenarios` 흐름은 본 작업과 무관하게 그대로 동작한다 (코드베이스 스캔 → 시나리오 → 액션매핑).
