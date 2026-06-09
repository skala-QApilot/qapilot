# 심층 의미적 역검증 보고서

> 외부 reviewer 시각 (구현자 아닌 제3자) 의 객관적 의미 검증.
> 표면 (단위 테스트 PASS) 이 아닌 본질 (실 use case 가 만족되는가) 점검.

---

## 0. 검증 방법

1. **5 public API import 가능성** — 호출자 시각의 시작점
2. **Pydantic 사이클 정합성** — 추출 → 직렬화 → DB/S3 → 역직렬화 → 호출자 사용
3. **실 환경 use case 시나리오** — Signup 환각 차단이 실제로 동작하는가
4. **회의 verbatim 결론 ↔ 구현 누락 점검**
5. **외부 의존성 (다른 영역 모듈) 호환** — 미완성/누락 의존 X
6. **표현/가독성** — 외부 공유 가능한 상태

---

## 1. ✅ Public API 사용 가능성

```python
from qapilot.scan.orchestrator import scan_all_metadata
from qapilot.shared.scan_storage import load_metadata_index, load_source
from qapilot.shared.db_state import get_db_snapshot_cached
from qapilot.shared.tv_validator import TVValidator
# 5 API 모두 정상 import
```

시그니처 검증:
- `scan_all_metadata(service_id, repo_root, commit_sha, *, skip_*, llm_client, exclude_dirs)`
- `TVValidator.validate(tv_field, scenario_intent, db_snapshot, schemas, schema_name)`

→ caller (유빈 agent / CLI / service register flow) 가 직접 호출 가능.

---

## 2. ✅ Pydantic 사이클 정합 — round-trip 검증

```python
idx = FrontendSelectorsIndex(...)
d = idx.model_dump(mode='json')
# kind/sub_kind/service_id/commit_sha/by_route 모두 보존
# /signup.inputs[0] = {testid, html_type, required, v_model, validators, ...}
```

PoC 3 의 `_to_json_bytes` 가 `sort_keys=True` 로 직렬화 → SHA256 결정성.
PoC 4 의 `load_metadata_index` 가 dict 그대로 반환 → caller 가 model_validate() 가능.

---

## 3. ✅ 실 use case e2e — Signup 환각 차단 시나리오

mini-bss-lite (commit 3fc4330) 한 호출 결과:

```text
scan_all_metadata:
  selectors=125, routes=16, schemas=51, patterns=129, errors=0

유빈 agent 시각 — Signup 시나리오 메타데이터:
  /signup inputs (testid):  ['name', 'email', 'password', 'birth_date']
  /signup buttons (testid): ['signup-submit']
  SignupRequest schema:
    email:    required=True, validators=[min_length, max_length, regex]
    password: required=True, validators=[min_length, max_length]
    name:     required=True, validators=[min_length, max_length]
    birth_date: required=True
    guardian_consent: required=False

TVValidator 통합:
  신규 email "test@example.com" + DB 없음 + signup intent
    → valid=True (schema/type/3 format/db_absence 모두 pass) ✅
  잘못된 email "not-an-email"
    → valid=False, reasons=['regex(^[^@\s]+@[^@\s]+\.[^@\s]+$) FAIL'] ✅
```

→ **환각 selector 차단 + TV 검증 의도 그대로 동작**.

---

## 4. ✅ 회의 verbatim 결론 누락 점검

| 회의 결론 | 데이터 layer 구현 | 비고 |
|---|---|---|
| 1. PRD → TS | (유빈 영역) | — |
| 2. HITL | (후순위) | — |
| 3. TS → TC | (유빈 영역) | — |
| 4.1 G/W/T/V | TVValidator (검증) | 생성 LLM = 유빈 |
| 4.2 DB → value | get_db_snapshot_cached + TVValidator | ✅ |
| 4.3.a github → S3 | dump_source_to_s3 + clone_repo_shallow | ✅ |
| 4.3.b codebase-index → S3 | (C 영역, 기존) | 변경 0 |
| 4.3.c 테스트 코드 스캔 | pytest_ast_parser + LLM 보강 | ✅ |
| 4.3.d 셀렉터/라우터/스키마 | 3 extractor | ✅ |
| 4.3.e namespace 분리 | metadata_indices (DDL + writer) | ✅ |
| 4.3.f 검색 → TC/TV | load_metadata_index + load_source | ✅ |
| 5. 흐름 통합 | scan_all_metadata orchestrator + 유빈 호출 | ✅ |
| 6. DB 스캔 → 비교 | db_state.cache + TVValidator.db_existence/absence | ✅ |
| TV pool entity | 🔵 유빈 영역 (회의 명시) | 데이터 layer 외 |

**누락 0건** — 데이터 layer 책임 범위 완전 커버.

---

## 5. ✅ 외부 의존 호환

- `qapilot.tools.db_test_tool.DBTestTool` (PoC 8) ✅
- `qapilot.shared.llm_client.LLMClient` (PoC 5.1) ✅
- `qapilot.db.connection.get_pool` (모든 writer/reader) ✅
- `qapilot.storage.s3_client` (head_object 추가 + 기존 함수) ✅

모두 import OK. 미완성/누락 의존 0.

---

## 6. ⚠️ 호출자 시각의 권장사항 (데이터 layer 외부 책임)

### 6.1 LLM prompt 가공 (유빈 영역)
`load_metadata_index` 응답은 Pydantic 의 raw `model_dump()` 결과 — `extracted_from`, `confidence`,
`extraction_method` 등 메타 필드 포함. 유빈 agent 가 LLM prompt 에 주입 시 다음 처리 권장:
- 메타 필드 제거 (token 절감)
- `None` 필드 제거 (노이즈 감소)
- testid / type / validators 핵심 field 만 추출

데이터 layer 는 의도적으로 raw 형태 보존 — 호출자가 사용 시점에 맞춰 가공.

### 6.2 S3 Lifecycle policy (운영 시점)
`docs/scan-enhancement/s3-path-spec.md` §4 의 정책 — `services/*/source/* 30일` 은 운영 배포
시 MinIO/S3 lifecycle rule 등록 필요. PoC 단계 무관.

### 6.3 multi-service DB URL 분리 (격차 12 후속)
현재 `DBTestTool._module_url()` 은 단일 `QAPILOT_SUT_DB_URL` 가정. multi-service 시점에
service 별 분기 필요. PoC 8 의 cache key 는 `service_id` 포함 — DB URL 분기 추가 시 호환.

---

## 7. 최종 결론

### 7.1 데이터 layer 7 components 모두 정상

| Component | 모듈 | 상태 |
|---|---|---|
| 추출 (4영역) | 5 extractor + node-bridge | ✅ |
| 저장 | metadata_writer + s3_client.head_object | ✅ |
| 조회 | scan_storage + metadata_reader | ✅ |
| 검증 | tv_validator | ✅ |
| DB cache | db_state | ✅ |
| Source dump | source_dumper | ✅ |
| Orchestration | orchestrator | ✅ |

### 7.2 의미적 정합

- e2e 시나리오 (Signup) 정상 동작
- LLM 환각 차단 + TV 의도 검증 가능
- 결정성 (sort_keys + AST + LRU + LLM temp=0 + TTL) 5중 안전장치
- backward compatibility — 기존 codebase_indices / DBTestTool / s3_client 영향 0

### 7.3 외부 공유 가능 상태

- docs 14 파일 — 1인칭 표현 제거, 디버깅 흔적 없음
- code docstring — 1인칭 표현 제거, "Author: 주환 (kimjuhwan)" 표기
- 단위 테스트 231/231 PASS 유지

**데이터 layer 작업 완전 종료**. 후속 작업 (framework 확장 / 운영 lifecycle) 은 별도 시점.
