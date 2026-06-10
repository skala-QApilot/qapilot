# 보안 고려사항 — TV 단계 코드베이스/DB 연결

> 본 작업이 DB 실제 데이터와 코드베이스 메타데이터를 LLM 에 전송하기 때문에, 어떤 보안 위험이 있고 어떻게 대응했는지 정리.

---

## 1. 위험 매트릭스

| 위험 | 발생 시나리오 | 영향 | 본 작업의 대응 |
|---|---|---|---|
| **민감 컬럼 LLM 노출** | DB 에 `password_hash`, `token` 등이 있고 그게 LLM context 로 전송됨 | LLM provider 의 학습/로그/캐시에 흡수 가능성 | `sensitive_mask.strip_sensitive_from_db_snapshot` 으로 사전 제거 |
| **마스킹 값 → 빈 string 실행** | LLM 이 `***` 같은 마스킹된 값을 응답에 박아넣어서 그대로 실행됨 | 테스트 자체가 빈 값으로 실패 (PR #256 본질) | sensitive 필드를 LLM 에 **노출 자체 안 함**. placeholder 로 별도 저장 |
| **스키마 sensitive 표시 누락** | `backend_schema_parser` 가 `password` 키워드 외 컬럼을 못 잡음 (예: `user_secret`) | 새로운 sensitive 필드가 LLM 에 노출됨 | 키워드 fallback (`_SENSITIVE_KEYWORDS`) 11종 + schema spec 의 `sensitive=True` 두 단계 검사 |
| **민감 데이터 PR/로그 누출** | 단위 테스트 출력, S3 metadata-index 캐시, agent_logs 에 sensitive 값이 잔존 | 로그 흡수 시 누출 | `metadata_indices` 의 메타데이터는 **schema 정보만** 보관 (실 데이터 0). DB snapshot 은 process memory 만 (디스크/S3 미저장) |
| **GitHub repo 의 secret 코드** | 추출 대상 코드에 hardcoded API key 가 있으면 source/ S3 에 그대로 PUT | S3 access 가능한 사람에게 노출 | source/ 에는 코드 자체 (text) PUT — 별도 secret 스캔 없음. 운영 시점에 별도 도구 (gitleaks 등) 권장 |

---

## 2. Option α — sensitive 필드 LLM 완전 제외

### 채택 이유 (다른 옵션과의 비교)

| 옵션 | 동작 | 위험 |
|---|---|---|
| α (채택) | sensitive 필드를 LLM 컨텍스트에서 완전 제외. TC.values 에는 `${TEST_PASSWORD}` placeholder | 가장 안전 |
| β | LLM 에 마스킹된 형태 (예: `***`) 보임 + 응답 unmask | **PR #256 본질** — LLM 이 `***` 그대로 응답에 박을 위험 |
| γ | 마스킹 안 함 — 실제 password LLM 노출 | 데이터 누출 + 학습 흡수 |

### α 의 핵심 구현

`qapilot/shared/sensitive_mask.py`:

```python
# 1. sensitive 필드 식별 (schema.sensitive=True + 키워드 fallback)
sensitive_names = get_sensitive_field_names(filtered_schemas)

# 2. LLM 호출 전 — schemas 에서 sensitive 필드 제거
sanitized_schemas = strip_sensitive_from_schemas(filtered_schemas)
# DB snapshot 에서도 sensitive 컬럼 제거
sanitized_rows = strip_sensitive_from_db_snapshot(rows, sensitive_names)

# 3. LLM 호출 — sensitive 필드 자체가 컨텍스트에 없음
response = await llm.chat(... sanitized_schemas, sanitized_rows ...)

# 4. LLM 응답 received — 만약 LLM 이 우연히 sensitive 필드를 응답에 포함시켰어도
#    placeholder 가 덮어쓴다 (안전 장치 이중)
sensitive_entries = build_sensitive_value_entries(sensitive_names)
final_values = merge_values_with_sensitive(llm_values, sensitive_entries)
# → sensitive 필드는 항상 placeholder
```

### LLM 응답에 sensitive 가 들어왔을 때 — 이중 안전

LLM 이 prompt 의 명시적 지시 ("sensitive 필드를 응답에 포함하지 마라") 를 어기더라도, `merge_values_with_sensitive` 가 LLM 응답의 sensitive 필드를 placeholder 로 덮어씌운다. → **이중 안전 장치**.

---

## 3. PR #256 의 본질 — 재발 방지 정합

### PR #256 의 문제 (이전 실수)

CodeGenerator 가 LLM 호출 시 password 같은 민감 값을 `process.env.E2E_USER_PASSWORD` 로 마스킹:

```javascript
// generated_code.js
await page.getByTestId("password").fill(process.env.E2E_USER_PASSWORD);
```

→ pipeline 의 `_resolve_js_value` 가 환경변수 미설정 시 `""` 반환 → `form.password.fill('')` → HTML5 required validation fail → POST 0건.

### PR #256 의 fix

ActionMapping 의 원본 value fallback:
```python
if generated_codes:
    exec_mapping = _action_mapping_from_generated_code(item)
    original = action_mapping_by_tc.get(str(tc_id)) or {}
    original_steps = list(original.get("steps") or [])
    for idx, step in enumerate(exec_mapping.get("steps") or []):
        if idx < len(original_steps):
            step["api_endpoint"] = original_steps[idx].get("api_endpoint")
            orig_value = original_steps[idx].get("value")
            if orig_value and not step.get("value"):
                step["value"] = orig_value  # ← 빈 값이면 원본 fallback
```

### 본 작업이 그대로 활용

본 작업의 TC.values 에 들어가는 placeholder (`${TEST_PASSWORD}`) 는 PR #256 의 ActionMapping fallback 으로 실행 시 실제 값이 들어간다.

```
TC.values: [{field: password, value: ${TEST_PASSWORD}, sensitive: true}]
  ↓ (액션매핑 단계)
ActionMapping.steps[]: [{value: ${TEST_PASSWORD}}]
  ↓ (CodeGenerator → generated_code)
generated_code.js: await page.getByTestId("password").fill(process.env.TEST_PASSWORD)
  ↓ (실행 시점 — pipeline._resolve_js_value)
실제 환경변수 TEST_PASSWORD 값으로 치환
  ↓
form.password.fill("actual_password") — HTML5 검증 통과
```

→ PR #256 의 fix 가 그대로 동작.

---

## 4. 운영 시점 추가 권장

### 4.1 환경변수 명명 규칙
TC.values 의 sensitive placeholder = `${TEST_<FIELD_UPPERCASE>}` 형식.
운영 환경에서 다음 환경변수 등록 필요:
- `TEST_PASSWORD`
- `TEST_API_KEY`
- `TEST_TOKEN`
- 등등 (필요한 sensitive 필드 종류만큼)

### 4.2 secret 스캔 (옵션)
본 작업의 `dump_source_to_s3` 가 코드를 그대로 S3 에 PUT 한다. 운영 시점에 다음 추가 권장:
- 코드베이스에 hardcoded secret 이 있으면 source/ 에 그대로 들어감
- `gitleaks` / `trufflehog` 같은 도구로 사전 스캔 후 PUT 권장
- 본 작업 범위 외

### 4.3 S3 access control
`metadata-index/` 와 `source/` 가 같은 bucket 에 저장. multi-tenant 시:
- service 별 prefix 격리 (이미 적용)
- AWS S3 IAM Policy 분리 (운영 시점)
- 본 작업 범위 외

### 4.4 LLM provider 정책 확인
sensitive 필드는 본 작업에서 제외하지만, schemas/selectors/patterns 자체에 비즈니스 로직이 노출됨.
LLM provider 의 데이터 보존 정책 확인 권장 (OpenAI 의 경우 API 사용분은 학습에 사용 안 함 — 단 확인 필요).

---

## 5. 검증 — sensitive 처리가 실제로 동작하는가

### 5.1 단위 테스트
`tests/test_sensitive_mask.py` 31 케이스:
- sensitive 필드 식별 (schema.sensitive + 키워드 fallback)
- schemas / db_snapshot 에서 sensitive 제거
- placeholder 생성 + 머지
- **LLM 응답이 sensitive 필드 포함 시 placeholder 가 덮어쓰는지** (이중 안전 검증)

### 5.2 실 환경 e2e (mini-bss-lite)
mini-bss-lite 의 `backend/app/models.py` 의 `Customer.password_hash` 가 sensitive 로 자동 마킹된다 (`backend_schema_parser` 의 `password` 키워드 휴리스틱).

본 작업 e2e 실행 후 산출물:
- TC.values 의 password 필드가 `${TEST_PASSWORD}` 인지 확인
- LLM prompt 의 schemas dict 에 password 가 없는지 확인 (로그)
- DB snapshot 에 password_hash 가 없는지 확인 (로그)
