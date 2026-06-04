# PRD/도메인 문서 SaaS 통로 격차 분석

> **상태**: OPEN
> **작성일**: 2026-06-04
> **담당**: A (본인, agent) + E (Spring) + F (UI)
> **메모리**: [[project_qapilot_prd_docs_saas_wire_gap]]
> **격차 12 후속 동형 패턴**: [[project_qapilot_saas_test_account_gap]] / `docs/web-direction-and-github-e2e-gap.md`

## 1. 격차 정의

QApilot SaaS 흐름에서 **사용자가 UI 로 업로드한 PRD/도메인 문서가 agent (Python) 의 ScenarioGenerator/RequirementExtractor 까지 전달되지 않음**. 3-tier 모두 부분 끊김 — 저장은 되는데 활용 못 함.

### 영향

- ScenarioGenerator 가 PRD 컨텍스트 0 → 시나리오가 코드 스캔만으로 LLM 추측 생성 (`requirements_count=0` + `rtm_using_ts_fallback`)
- PRD v4.0 같은 신규 비즈니스 룰이 시나리오에 미반영
- multi-tenant SaaS 본질 위반 — 모든 service 가 dev 단일 cfg.project.root 의 docs 공유 (옵션 a 우회 시)

### 검증된 실제 동작 (2026-06-04)

| trace | 상태 | 비고 |
|---|---|---|
| `3d2c9177` (06:16 KST) | `requirements_count=0` + `rtm_using_ts_fallback reason='empty requirements list'` | PRD 통로 끊김 — `cfg.project.root` 오타 / 미설정 |
| `b5cedb75` (06:44 KST) | `requirements_extracted count=41 confidence=0.95` + `scenarios_count=26 tc_count=156` | dev 우회 (옵션 a `cfg.project.root` 설정 + mini-bss-lite local clone) 적용 후 정상. **단 multi-tenant 본질 위반** |

## 2. 3-tier 현황 (저장은 되는데 활용 못 함)

### 2.1 UI (QApilot-UI) — 끊김 ❌

**`ServiceSetupPage.tsx:30, 281-291`**:
```tsx
const [uploadedFiles, setUploadedFiles] = useState<File[]>([]);
...
onClick={() => onGenerateScenarios({
  name: name.trim(),
  repos: githubEntries.filter(...).map(...),
  stagingUrl: stagingUrl.trim(),
})}
```
**`uploadedFiles` 가 React state 만, `POST /api/services/{id}/files` 호출 0건**. 사용자가 파일 선택해도 버려짐.

**`src/api/files.ts`**: `listFiles()` 만 구현, `uploadFile()` / `createFile()` export 없음.

### 2.2 Spring (qapilot-server) — 저장 OK / 전달 끊김 ⚠️

**`DomainFileController.java`** (저장 정상):
```java
@PostMapping  // POST /api/services/{serviceId}/files
public ApiResponse<...> create(@PathVariable String serviceId, @RequestPart("file") MultipartFile file) {
    return ApiResponse.ok(Map.of("file", domainFileService.create(serviceId, file)));
}
```

**`DomainFileService.create`**: S3 (`services/{id}/domain/{fileId}/v{ver}/{filename}`) + JPA (`domain_documents` 테이블) + 버전 관리 + `reflected` flag — **저장 본질 완성**.

**`AgentExecutionService.startScenarioGeneration` (line 30-43)** (전달 끊김):
```java
String traceId = fastApiAgentClient.startScenarioGeneration(
    service.serviceId(), service.qapilotDir(),
    request.trigger(), request.userInput(),
    request.scenarioIds(), request.filter(), request.tags(),
    service.repos()   // ← repos 만, files 없음
);
```
**`DomainDocumentRepository` 미참조 + FastAPI body 에 file metadata 동봉 안 함**.

**`ScenarioGenerationStartRequest` (record)**: `trigger` / `userInput` / `scenarioIds` / `filter` / `tags` 만. **`file_ids` 필드 없음**.

### 2.3 Python agent (qapilot) — read 통로 부재 ❌

**`agent_router.py` (`/scenario-generation` endpoint, line 100-124)**:
```python
options: RunOptions = {
    "command": "generate_scenarios",
    "trigger": trigger,
    "user_input": _optional_str(body, "user_input"),
    ...
}
_inject_git_options(body, options)
```
**body 에서 `file_ids` / `domain_files` 안 받음**.

**`pipeline.py:228-250 _doc_import`**:
```python
config = load_config()
proj = config.project
repo_root = Path(proj.root or proj.repo_path or ".")
docs_dir = repo_root / "docs"   # ← agent process 의 local 디스크만
```
**`state.qapilot_dir` 도 안 봄. S3 read 코드 없음**.

**`storage/s3_client.py`**: `put_bytes(80)`, `put_file(93)` 만 정의. **`get_object` / `download` 메서드 0건** — agent 가 S3 read 자체 불가능.

### 2.4 Spring 자체 추가 격차 (F 항목) ⚠️

**`DashboardService.java:52-65 domainFiles()`**:
```java
private List<String> domainFiles(Path qapilotDir) {
    Path domainDir = qapilotDir.resolve("domain");
    if (!Files.isDirectory(domainDir)) {
        return List.of();
    }
    ...
}
```
**`qapilot_dir/domain/` 디렉토리 read** 하여 UI dashboard "도메인 파일" 패널 표시.

그러나 **`DomainFileService.create` 가 S3+DB 만 저장, `qapilot_dir/domain/` 에 mirror 안 함** → **UI dashboard 의 도메인 파일 패널이 항상 빈 채로 표시**.

Spring 내부 자체 wire 끊김 — F 영역 1줄 fix 만으로 해결 가능.

## 3. 6 항목 정리 (A ~ F)

| # | 끊김 위치 | 영역 | 결정적 누락 | 후속 sub 이슈 |
|---|---|---|---|---|
| **A** | UI `files.ts` 에 `uploadFile(serviceId, file)` 미구현 + `ServiceSetupPage` onClick 가 호출 안 함 | F (UI) | `POST /api/services/{id}/files` 호출자 | sub-A |
| **B** | Spring `ScenarioGenerationStartRequest` 에 `file_ids: List<String>` 없음 | E (Spring) | DTO 필드 추가 | sub-B |
| **C** | Spring `AgentExecutionService` 가 `DomainDocumentRepository` 미참조 + FastAPI body 에 동봉 안 함 | E (Spring) | repository read + body 동봉 | sub-C |
| **D** | Python `agent_router` body 에서 `domain_files` 미수신 + `RunOptions` 필드 없음 | **A (본인)** | RunOptions/state 확장 + agent_router 주입 (격차 12 staging_url 동형) | sub-D |
| **E** | Python `s3_client.py` 에 `get_object` / `download` 없음 | **A (본인)** | S3 read API 추가 | sub-E |
| **F** | Spring `DomainFileService.create` 가 `qapilot_dir/domain/` mirror 안 함 → `DashboardService.domainFiles()` 빈 패널 + agent dev cfg.root 없이 read 못 함 | E (Spring) | 1줄 추가 (`Files.write(qapilotDir/domain/<filename>)`) | sub-F |

## 4. 해결 옵션

### 옵션 a — Spring F 만 (즉시, 임시 dev 우회)

- F 만 fix (1줄): `DomainFileService.create` 가 S3 PUT 후 `qapilot_dir/domain/<filename>` 에도 mirror
- agent 의 `_doc_import` / `_read_latest_prd_text` 가 `state.qapilot_dir/docs` 또는 `state.qapilot_dir/domain` 추가 분기 — 본인 PR-D' (1줄)
- 효과: UI dashboard 패널 정상 + agent read 가능 (dev cfg.root 없이도)
- 한계: A (UI 업로드) + B+C (Spring → agent body) 미해결 — 여전히 사용자가 UI 에서 업로드해도 저장 안 됨 (POST 호출자 0). 즉 F 만 fix 해도 본질 위반.

### 옵션 b — 본질 wire (SaaS multi-tenant 완전) ⭐ 정석

- A~F 모두 6 sub PR. 3-tier 협업 (F + E + A)
- 격차 12 (test_account state 패턴) 와 동형 패턴 — Spring → body 동봉, agent state 확장, _doc_import 가 state 우선 read

**PR 분할 계획**:

| sub PR | 영역 | 변경 | 추정 라인 |
|---|---|---|---|
| **PR-A** | F (UI) | `files.ts` 에 `uploadFile()` 추가 + `ServiceSetupPage` onClick 에서 호출 (또는 별도 단계) | ~50 |
| **PR-B** | E (Spring) | `ScenarioGenerationStartRequest` 에 `file_ids: List<String>` 추가 | ~5 |
| **PR-C** | E (Spring) | `AgentExecutionService.startScenarioGeneration` 가 `DomainDocumentRepository.findByServiceId()` 호출 + `FastApiAgentClient` 시그니처 확장 + body 에 `domain_files: [{file_id, s3_key, filename, mime}]` 동봉 | ~30 |
| **PR-D** | **A (본인)** | `RunOptions / PipelineState` 에 `domain_files: list[dict] | None` 추가 + `agent_router._start_pipeline` body 추출 + `_run_pipeline_task` / `runner.run_pipeline` 시그니처 확장 (격차 12 staging_url/test_account 동형 패턴) | ~80 |
| **PR-E** | **A (본인)** | `s3_client.py` 에 `get_object(key) -> bytes` + `download(key, path)` 추가 (boto3 wrapper, MinIO/AWS 동일) | ~30 |
| **PR-F** | **A (본인)** | `pipeline._doc_import` 가 `state.domain_files` 우선 → S3 download → tmp 디렉토리 → 기존 `_read_latest_prd_text` / `DomainKnowledgeTool` 활용. cfg.project.root fallback 유지 (CLI 호환) | ~60 |

본인 A 영역 = PR-D + PR-E + PR-F (3건). 다른 영역 의존:
- PR-D 가 PR-C (Spring body) 필요 — 단 body 미주입 시 None → graceful skip (격차 12 동형 호환 유지)
- PR-E 가 PR-F 의존 (s3_client read 후 _doc_import 가 사용)

### 옵션 c — 하이브리드 (옵션 a + 옵션 b 의 일부)

- F 즉시 (Spring 1줄) + agent D' (1줄) → 즉시 효과 (dev cfg.root 없이도 작동)
- A + B + C + D + E + F-본질 분할 PR 별도 진행
- 우선순위 a 즉시 + b 점진. **추천**.

## 5. 우선순위

**P0** — 옵션 c 하이브리드 진행:
- **F (Spring 1줄)** + **D' (agent 1줄)** 즉시 fix — dev 우회 (cfg.project.root 설정 강제) 없이도 정상 동작
- 사용자 부담 감소 + dev cfg multi-tenant 위반 해소

**P1** — 옵션 b 의 A + B + C 점진:
- UI 업로드 wire (UI 팀)
- Spring DTO + body 동봉 (kshyun E)
- agent 본인은 PR-D/E/F 본질 진행 (격차 12 SaaS test_account 동형 패턴)

**P2** — `reflected: boolean` 필드 활용:
- 사용자가 어떤 파일 버전을 "반영" 으로 표시했는지 agent 가 인식 → 미반영 PRD 제외

## 6. 영역 협업 매트릭스

| 영역 | 담당 | 작업 PR | 의존 |
|---|---|---|---|
| **F (UI)** | 미배정 (UI 팀) | PR-A | 없음 |
| **E (Spring)** | kshyun | PR-B / PR-C / PR-F (Spring mirror) | PR-A (body 받기) |
| **A (agent)** | 본인 (jkwltx177) | PR-D / PR-E / PR-F (agent read) | PR-C (Spring body 동봉) |

본인 D+E+F 는 **격차 12 (test_account state 패턴)** 와 동형 — `RunOptions / state` 확장 + agent_router 주입 + pipeline 우선순위 fallback. Spring 측 변경 표면 0 (body 미주입 graceful) 정책 유지.

## 7. 검증 절차

각 sub PR 머지 후:

1. **A 머지** → UI 에서 PRD 업로드 → 네트워크 탭에 `POST /api/services/{id}/files` 200 OK + Spring DB `domain_documents` row 1건 + MinIO `services/{id}/domain/` 객체 1건
2. **B + C 머지** → SaaS scenario-generation 호출 body 에 `file_ids: ["..."]` 확인 + agent log 에 `body.domain_files` 수신
3. **D + E + F 머지** → `_doc_import` 가 S3 download → tmp 디렉토리 → `doc_import_done stored=N` 로그 + `requirements_extracted count=N` (PRD 텍스트 길이 > 0)
4. **F (Spring mirror) 머지** → UI dashboard "도메인 파일" 패널에 업로드한 파일 표시 + agent 가 cfg.project.root 없이도 read 가능

## 8. 관련 메모리 + 격차

- **[[project_qapilot_saas_test_account_gap]]** — 격차 12 SaaS test_account state 패턴 (동형). 본인 격차 N (PRD/docs) 가 같은 패턴으로 해결 권장
- **[[project_qapilot_06_02_white_screen_root_cause]]** — 06-02 SaaS 첫 e2e 진단 (PRD wire 끊김 부분 포함, §추가 발견 #5)
- **이슈 #128 후속** (frontend.json SaaS 미생성) — 같은 origin (codebase scan 시 docs 도 미반영) 일 수도. 별도 격차로 진행
- **이슈 #164 / PR #165** (격차 12 SaaS 후속 docs) — 본 격차 12 docs 동형 패턴 참조

## 9. 우선 액션

1. 본 docs 머지 + 본인 A 영역 PR-D/E/F 발의 가능 시점 합의 (의존 PR-C 머지 후)
2. UI 팀 (PR-A) + Spring 팀 (PR-B/C/F-mirror) 발의 의향 확인
3. 임시 대증: 본인 docs PR 후속 — `DomainFileService.create` 의 mirror 1줄 (옵션 c F) Spring 팀이 즉시 진행 가능
