# 웹 방향 전환 + GitHub e2e 현황 갭 분석 (2026-05-22)

> **목적**: 2026-05-22 교수님 피드백 회의에서 결정된 *"완전 웹 + GitHub 연동 e2e"* 방향이 현재 develop 코드에서 실제로 가동 가능한지 정량 점검. Tool 본체 / Pipeline 분기 / Entry point 3-tier 분석 + 격차 + 백업 플랜 + 영역별 작업 분배 정리.
>
> **회의 결정 출처**: 교수님 피드백 회의록 (2026-05-22)
> **점검 시점**: develop `55e6d0e` (PR #163 머지 직후, PR #162 GitCodebaseScannerTool 품질 fix 머지 후)

---

## 1. 회의 결정 요약 — 방향 전환

### 1.1 핵심 결정

| 항목 | 결정 |
|---|---|
| **방향성** | 완전 웹 + GitHub 연동 e2e (**추후 고도화 목표**) |
| **현재** | CLI 는 유지만 (격하 X, 신규 작업 우선순위 ⬇). 방향성을 웹으로 틀었음 |
| **코드베이스 접근** | GitHub REST API 연동. 방화벽 예외 신청으로 URL/IP 풀어서 접근 |
| **DB 스캔** | 같은 K8s 클러스터에 모듈 심어 접근. "스테이징 접속 가능한 환경" 전제 |
| **DB 계정/권한** | "권한 받아서 진행" 전제만 명시. 세부 권한 관리 = 고도화 |
| **발표 전략** | 되는 환경 기준 + 백업 플랜 + 고도화 |

### 1.2 보안 환경 배경 (피드백 요약)

- 기업 PC 에 소프트웨어 감시 툴 → 허가되지 않은 설치 차단
- 산업보안 강한 곳 (SK하이닉스/삼성 등) — 깡통 PC + VM + 폐쇄망 + 파일 업로드 차단
- VDI 환경도 개발/업무/인터넷 분리 — 개발 VDI 는 막힘
- **반면** GitHub URL/IP 같은 외부 API 호출은 방화벽 예외 신청으로 풀 수 있음 — 오히려 설치보다 일반적

### 1.3 환경별 가능 여부

| 환경 유형 | DB 접근 |
|---|---|
| 일반 멤버사 (가스/케미칼 등) | 비교적 자유 — IP 만 열어주면 가능 |
| 금융권 | 폐쇄망이지만 특정 IP 풀 가능. 전용 PC 에서만 작업 |
| 산업보안 강한 곳 (SK하이닉스/온 등) | 별도 서버. 개인 PC 스테이징 다이렉트 접속 불가 |

---

## 2. 정확한 아키텍처 — Central Hub 모델

회의 결정 + 현재 develop 의 `qapilot/api/` 구조 + `orchestrator/runner.py` 공유 진입점 분석 종합.

```
┌─────────────────────────────────────────────────────────────┐
│ [외부 사용자 — 회사 사무실 PC]                                  │
│   웹 UI 접속 → 프로젝트 등록                                     │
│   사용자 입력: GitHub URL, SUT staging URL, branch 등           │
│   ⚠️  token 정책 미정 (우리 측 vs 사용자 측 미결정)               │
│   ❌ framework 입력 X — Central Hub 가 repo 종합 분석 후 자동 추론│
└───────────────────────────┬─────────────────────────────────┘
                            ↓ /api/projects/register
┌─────────────────────────────────────────────────────────────┐
│ [qapilot 서버 — Central Hub, 이미 배포 + 고정 URL]              │
│                                                              │
│  1. ProjectRegistry → 사용자별 프로젝트 등록                     │
│  2. 서버 측 디렉토리 생성 (project_slug 단위)                    │
│  3. qapilot.config.yaml 저장                                  │
│     project:                                                  │
│       repo_url: https://github.com/user/their-app           │
│       token: <TBD>  ← 우리 측 공용 vs 사용자 입력 정책 미정       │
│       branch: main                                            │
│       target_url: https://staging.user-company.com           │
│       framework: <자동 추론>  ← GitCodebaseScannerTool 출력      │
│       language: <자동 추론>   ← 동일                              │
│                                                              │
│  4. /api/agent/scenario-generation 호출                       │
│     body = { service_id, qapilot_dir }                       │
│     서버 = qapilot_dir → qapilot.config.yaml 로드 →            │
│       모든 정보 (repo_url, target_url 등) 자동 획득             │
│                                                              │
│  5. orchestrator/runner.py 실행 (CLI/API 공유)                  │
└───────────┬──────────────────────────────────┬──────────────┘
            ↓                                  ↓
    [GitHub REST API]                  [Target SUT staging]
    ←─ 방화벽 예외 신청 URL/IP            ←─ Playwright 접근
       (사용자 회사 GitHub 접근)              (사용자 회사 staging)
```

**4-actor 모델**:
1. **외부 사용자** — 회사 사무실 PC, 웹 UI 만 접근
2. **qapilot 서버** — Central Hub, 이미 배포 + 고정 URL (`api/main.py` FastAPI)
3. **GitHub** — 외부 사용자의 회사 GitHub (REST API)
4. **Target SUT staging** — 외부 사용자의 회사 staging 환경 (Playwright)

---

## 3. 현재 develop 의 Git 코드베이스 연동 — 3-Tier 상태

### 3.1 Tier 1 — Tool 본체 (대체로 완비 ✅, 단 manifest RDB 미구현)

#### 회의록 2 — FR-000 Git 코드 스캔 설계 결정 (2026-05-21, 6 항목) 정합

| # | 회의 결정 | 현 develop | 갭 |
|---|---|---|---|
| 1 | **파일 복제 후 수정** — 기존 `codebase_scanner_tool.py` 유지 + `git_codebase_scanner_tool.py` 신규. AST 파싱 (py/ts/js/java/yaml) 전체 재사용 | ✅ 두 파일 공존. AST 로직 재사용 | 정합 |
| 2 | **REST API** + 메모리 처리 + 원본 디스크 저장 X | ✅ REST API 호출, `base64` 디코딩 후 메모리 파싱 | 정합 |
| 3 | **어댑터 구조** (GitHub/GitLab + 추후 Bitbucket) | ✅ `GitPlatformAdapter` ABC + 2 구현. URL 파싱 자동 선택 | 정합 |
| 4 | **manifest RDB 전환** — `.qapilot/manifest.json` 제거, `last_commit_hash` RDB 저장 | 🔴 **선언만 + 미구현**. Tool docstring 만 *"manifest는 RDB(pipeline.py 담당)"* (`git_codebase_scanner_tool.py:5`). 실제: `models.py` 에 Project 테이블 없음 (`RequirementRecord` 하나만), pipeline 에 RDB 저장 코드 0건, `ProjectRegistry` = JSON 파일 (`.qapilot/projects/registry.json`) | **격차 8 신설** |
| 5 | **Git 이력 (blame/diff) RDB 캐싱** — FR-010 원인 추론 품질 유지 | 🔴 **미구현**. `ScanResult.git_diff` 가 메모리에만 (`pipeline.py:_save_codebase_index_to_disk` 가 디스크 5 파일만, blame/diff RDB 저장 layer 부재) | **격차 9 신설** |
| 6 | **멀티 레포** — 완료 | ✅ `params["repos"]` 완비 (PR #146 + #162) | 정합 |

#### Tool 본체 산출물 표

| 항목 | 위치 | 상태 |
|---|---|---|
| **GitCodebaseScannerTool** | `qapilot/tools/git_codebase_scanner_tool.py` | ✅ PR #146 + #162 |
| GitHub/GitLab 어댑터 (`GitPlatformAdapter` ABC) | line 120/164/243 | ✅ 다중 플랫폼 |
| Bearer token 인증 | line 174 | ✅ |
| tree-sitter 파싱 (py/js/ts/java/yaml) | line 26-30 | ✅ |
| Multi-repo (`params["repos"]`) — MSA 지원 | line 484-509 | ✅ |
| 증분 스캔 (`last_commit_hash` + HEAD~1 폴백) | line 417 + PR #162 | ✅ (단 `last_commit_hash` 영속 저장 없어 외부 입력 의존 — 격차 8) |
| `scan_status` 메타 (`full/incremental`) | line 461-465 (PR #162) | ✅ |
| **CodebaseScannerTool** (로컬 walk) | `qapilot/tools/codebase_scanner_tool.py` | ✅ — **회의 정합** (개발자 트랙 = 로컬 walk 유지. PR #157 분기로 외부 사용자 ↔ 개발자 분리) |
| **manifest RDB 영속화** | (없음) | ❌ **격차 8** |
| **Git 이력 RDB 캐싱** (blame/diff) | (없음) | ❌ **격차 9** |

### 3.2 Tier 2 — Pipeline 분기 (완비 ✅, 단 입력 의존)

| 항목 | 위치 | 상태 |
|---|---|---|
| `_codebase_scan` 분기 (PR #157) | `qapilot/orchestrator/pipeline.py:267-297` | ✅ |
| 분기 정책 | `is_git_mode = bool(run_options.get("repo_url") or run_options.get("repos"))` | ✅ |
| Git 모드 → `GitCodebaseScannerTool` | line 277-279 | ✅ |
| 로컬 모드 → `CodebaseScannerTool` (임시 fallback) | line 280-283 | ✅ |
| `_save_codebase_index_to_disk` (codebase-index/) | line 291 | ✅ |
| **⚠️ Tier 3 의존**: `run_options.repo_url` 채워지지 않으면 항상 `is_git_mode=False` | line 269 | ⚠️ 잠복 |

### 3.3 Tier 3 — Entry point (미연결 ❌)

| Entry | 위치 | repo_url 처리 | 격차 |
|---|---|---|---|
| **CLI** `generate scenarios` | `qapilot/cli/main.py:51-66` | ❌ `--repo-url` 옵션 없음. RunOptions 안 채움 | repo_url 채우는 layer 없음 |
| **Web API** `/api/agent/scenario-generation` | `qapilot/api/agent_router.py:40-55` | ❌ body 에서 추출 X. RunOptions 안 채움 | 동일 |
| **Web API** `/api/projects/register` | `qapilot/api/projects_router.py:19-26` | ❌ `repo_url` 필드 자체 없음. `local_path/config_path/index_path/framework/language` 필수 (CLI 가정) | 외부 사용자가 GitHub repo URL 만으로 등록 불가. **framework/language 입력 항목 X — Central Hub 가 자동 추론으로 채워야 함** |
| **ProjectConfig** (qapilot.config.yaml) | `qapilot/shared/config.py:70-74` | ❌ 필드 = `name/target_url/root/repo_path`. **`repo_url/branch` 부재. token 정책 미정** | qapilot.config.yaml 에 GitHub 정보 저장 모델 부재 |
| **ProjectRecord** (project_registry) | `qapilot/shared/project_registry.py:15-26` | ❌ `repo_url` 필드 부재. `framework/language` 필드는 있음 (단 사용자 입력 X → 자동 추론 채우기로 의미 전환) | 동일 |
| **RunOptions** (스키마) | `qapilot/shared/schemas.py` | ✅ `repo_url/token/branch/local_path/repos` 필드 정의됨 (단 안 채움) | 정의만 있고 채우는 layer 없음 |

#### ⚠️ 미결정 사항 — 사용자 입력 vs Central Hub 자동 추론

| 항목 | 출처 | 사용자 입력? | 현 develop 처리 | 비고 |
|---|---|---|---|---|
| `repo_url` | 사용자 | ✅ 필수 입력 | `params["repo_url"]` 필수 | GitHub URL |
| `branch` | 사용자 | ✅ 필수 입력 | `params.get("branch", "main")` ✅ | 기본값 `main` 자동 |
| `target_url` | 사용자 | ✅ 필수 입력 | `cfg.project.target_url` (qapilot.config.yaml) | SUT staging URL |
| `token` (GitHub) | **TBD** | ⚠️ **미정** | `params.get("token", "")` — **사용자별 입력 가정. default 빈 문자열 = public repo unauthenticated (GitHub rate limit 60/시간)**. "우리 측 공용" 정책 미구현 | (a) 우리 측 공용 (GitHub App / 서비스 계정) — secret store + `_normalize_repos` fallback layer 신설 필요 vs (b) 사용자별 PAT 입력 — projects/register 필드 + qapilot.config.yaml 저장 (보안 검토). **정책 결정 후 코드 정합**. PR #146/#162 본문에도 정책 명시 없음 |
| `framework` | Central Hub | ❌ 입력 X | ✅ `_detect_language_framework()` 휴리스틱 완비 (pom.xml→spring / pyproject.toml→fastapi·django·flask / package.json→react·vue·next 등). **multi-repo 시 `","join(...)` 결합 형태** (`"fastapi,react"`) | GitCodebaseScannerTool 이 repo 종합 분석으로 자동 추론. ProjectRecord 의 framework 가 단일 string 이면 **multi-repo 표현 정책 결정 필요** |
| `language` | Central Hub | ❌ 입력 X | ✅ 동일 (`_LANG_MAP` 카운트 top language) | 동일. multi-repo 콤마 결합 |

#### 🔑 API Key / Token 정책 — 종류별 구분

토큰은 두 종류 — **혼동 주의**:

| 종류 | 정책 | 출처 | 현 develop 처리 |
|---|---|---|---|
| **OpenAI API key** (`OPENAI_API_KEY`) | ✅ **확정 — Central Hub 서비스 측 제공** (사용자 입력 X) | Central Hub 환경변수 (서버 측 `.env` 또는 secret) | `langchain_openai.ChatOpenAI` 가 환경변수 자동 로드 (`qapilot/shared/llm_client.py:121`) + `load_dotenv()` (`config.py:89`). **현 코드는 cwd `.env` 의존 — CLI 사용자별 가정**. Central Hub 채택 시 **서버 환경변수 설정 하나로 자동 작동** (코드 변경 거의 없음) |
| **GitHub PAT** (`params["token"]`) | ⚠️ **미정 — 격차 6 (회의 결정 P0)** | (a) 우리 측 공용 GitHub App / 서비스 계정 vs (b) 사용자별 PAT 입력 | `git_codebase_scanner_tool.py:497` `params.get("token", "")`. **사용자별 입력 가정. default 빈 문자열** |

**SaaS 표준 패턴 정합**:
- ChatGPT / Claude.ai / Cursor 등 = 서비스 측 OpenAI key + 사용자 요금제로 비용 분배 → QApilot 동일 패턴
- 외부 사용자 = 웹 UI 사용 → 자기 OpenAI key 입력은 SaaS 표준 위반 + UX 저하

**비용 추적**:
- `LLMClient.total_input_tokens / total_output_tokens / total_cost_usd` 이미 trace 단위 측정 ✅
- 사용자별 회계 (요금제) = 향후 고도화 (Stage 4 의 운영 자동화 layer)
- `LLMConfig.monthly_budget_usd = 500` 이미 정의됨 (현재는 단일 예산)

**개발자 트랙 (CLI 로컬 e2e)**:
- 현재처럼 자기 `.env` 사용 (mini-bss-lite/.env + qapilot/.env 실측 패턴)
- 이슈 #135 의 `OPENAI_API_KEY` step 신설 = CLI 개발자 보조 진입점 한정 의미. 외부 사용자 UX 와 무관 (외부 = 서버 측 키 자동 사용)

### 3.4 실 e2e 검증 결과

```
GitCodebaseScannerTool 발동 경로 (현 develop):

1. CLI `qapilot generate scenarios` → ❌ Git 모드 진입 불가 (옵션 없음)
2. Web API /api/agent/scenario-generation → ❌ Git 모드 진입 불가 (body 추출 안 함)
3. 단위 테스트 (test_git_codebase_scanner_tool.py) → ✅ 53 passed (PR #162)
4. pipeline 단위 테스트 (test_pipeline_codebase_scan_fallback_156.py) → ✅ 4 passed
   - Git 모드 진입은 mock 으로만 검증 (run_options.repo_url 주입)

⚠️ 실 e2e 에서 GitCodebaseScannerTool 가 한 번도 호출된 적 없음 (단위 테스트만).
```

---

## 4. 격차 — "추후 완전 웹 + GitHub e2e" 진입 전제 조건

### 4.1 격차 표

| # | 격차 | 영역 | 우선순위 |
|---|---|---|---|
| **1** | `ProjectConfig` 에 `repo_url/branch` 필드 추가 (token 정책 결정 후 필드 형태 확정) | A+C 공유 (`shared/config.py`) | P1 |
| **2** | `ProjectRecord` + `/api/projects/register` 가 외부 사용자 입력 수용 + 서버 측 qapilot.config.yaml 자동 생성. **단 사용자 입력 = `repo_url/branch/target_url` 만. `framework/language` 는 Central Hub 가 GitCodebaseScannerTool 출력으로 자동 채움** | C 영역 (`api/projects_router.py` + `shared/project_registry.py`) | P0 |
| **3** | `/api/agent/*` 엔드포인트가 qapilot.config.yaml 로드 → `RunOptions` 자동 채움 (또는 pipeline 노드가 cfg 에서 직접 읽기) | A+C 공유 (`api/agent_router.py` + `orchestrator/pipeline.py`) | P0 |
| **4** | `_codebase_scan` 분기 — `cfg.project.repo_url` fallback 추가 (이중 출처: RunOptions 우선 + cfg fallback) | A (`orchestrator/pipeline.py`) | P1 (격차 1/3 후) |
| ~~**5**~~ | ~~GitHub webhook 자동 trigger~~ | ~~DevOps~~ | **🚫 폐기 — 회의 결정 2 §5: 웹훅은 폐쇄망 환경에서 동작 X. 수동 트리거 (대시보드 "코드 변경 스캔" 버튼) 유지** |
| **6** (정책) | **GitHub token 정책 결정** — (a) Central Hub 공용 token (GitHub App / 서비스 계정, secret store + `_normalize_repos` fallback layer 신설) vs (b) 사용자별 PAT 입력 (projects/register 필드 + qapilot.config.yaml 저장 + 보안 검토). **현 코드 = (b) 사용자별 가정으로 작성**. 결정 후 격차 1/2 의 token 처리 방식 확정. 참고: OpenAI API key 는 별개 — 서비스 측 (Central Hub) 으로 확정 (§3.3 API Key/Token 정책 표 참조) | 회의 결정 | P0 (격차 1/2 선행) |
| **7** (정책) | **multi-repo framework/language 표현 정책** — GitCodebaseScannerTool 이 multi-repo 시 `","join(...)` 결합 형태 출력 (`"fastapi,react"`). ProjectRecord 가 단일 string 필드면 (a) 콤마 그대로 저장 vs (b) repo 별 분리 필드 vs (c) primary framework 만 추출. **현 코드 = (a) 콤마 결합 그대로** | 회의 결정 | P1 (격차 2 동반) |
| **8** | **manifest RDB 전환 미구현** — `git_codebase_scanner_tool.py:5` docstring 만 *"manifest는 RDB(pipeline.py 담당)"* 선언, 실제 `models.py` 에 Project/Manifest 테이블 없음 + pipeline 에 `last_commit_hash` RDB 저장 코드 0건. 외부 사용자가 매 호출마다 `last_commit_hash` 직접 넘겨야 증분 스캔 작동 (현실적으로 불가) | A (`pipeline.py` 저장 책임) + C (`models.py` Project 테이블 신설) | **P0** — 회의 결정 2 §4 명시되었으나 실 코드 갭 큼 |
| **9** | **Git 이력 (blame/diff) RDB 캐싱 미구현** — 회의 결정 2 의 *"Git 이력은 FR-000 스캔 시점에 함께 추출하여 RDB 캐싱. FR-010 원인 추론 품질 유지"* 의도. 현 develop: `ScanResult.git_diff` (GitDiff TypedDict 포함 blame) 가 메모리만, 디스크/RDB 저장 layer 부재 | A (`_save_codebase_index_to_disk` 다음 RDB 저장 layer) + C (models 테이블) + F (RootCauseAgent 가 RDB 로드) | P1 (격차 8 후) |

#### 확정 사항 (격차 아님)

| 항목 | 내용 |
|---|---|
| **수동 트리거 (FR-016)** | 대시보드 "코드 변경 스캔" 버튼. `trigger=init/code_change/doc_update/natural_lang` 4종 값으로 분기. 현 develop 정합 ✅ (담당: A + C 대시보드 버튼) |

### 4.2 결과 — e2e 가능 여부

```
외부 사용자 가상 시나리오 (격차 미해소 시):

┌─────────────────────────────────────────────────────────────────┐
│ 1. 웹 UI 접속 → GitHub repo URL + SUT URL + branch 입력           │
│    (token 정책 미정 / framework 는 Hub 자동 추론)                  │
│    → ❌ projects/register 에 repo_url 필드 없음 → fail              │
│ 2. (가정 통과 시) /api/agent/scenario-generation                  │
│    → ❌ qapilot.config.yaml 에 repo_url 저장 모델 부재               │
│    → ❌ agent_router 가 RunOptions 안 채움                          │
│ 3. pipeline._codebase_scan → is_git_mode=False (repo_url 없음)    │
│    → ❌ CodebaseScannerTool (로컬 walk) → 서버에 코드 없음 → fail   │
└─────────────────────────────────────────────────────────────────┘

결론: 현 develop = "Tool 본체 완비, 단 e2e 호출 경로 미연결" 상태.
       격차 1~4 모두 해소되어야 외부 사용자 e2e 가동.
```

---

## 5. 백업 플랜 — 환경별 + 발표 답변 전략

### 5.1 발표 전략 (회의 결정)

- **기본 전제**: "스테이징 접속 가능한 PC 에서 작업한다"
- **답변 패턴**: *"되는 환경을 기준으로 만들었고, 만약 안 되는 환경이면 아키텍처 일부만 수정하면 되게끔 되어 있습니다."*
- **백업 플랜 언급**: *"클러스터 보안이 강화될 때는 이런 방법으로 할 수 있습니다"* 정도
- **고도화 위임**: 기간 짧으니 할 수 있는 것 기준 + 못할 때의 대응은 고도화

### 5.2 환경별 백업 플랜

| 환경 | 권장 모드 | 가능성 |
|---|---|---|
| 일반 멤버사 (가스/케미칼) | Web (GitHub e2e) ✅ + DB 클러스터 모듈 | 자유 |
| 금융권 | Web (GitHub e2e) ⚠️ + DB 전용 PC 모듈 | 폐쇄망 + IP 풀 |
| 산업보안 강한 곳 (SK하이닉스/온) | Web 어려움. **CLI 로컬 walk = 백업 플랜** | 별도 서버 가정 |
| 개발자 로컬 e2e (mini-bss-lite) | CLI + `CodebaseScannerTool` (PR #157 로컬 fallback) | ✅ — 개발자 트랙 |

### 5.3 DB 권한 전제

- **기본 전제**: *"DB 접속 권한을 받아서 진행합니다"*
- 세부 권한 관리 (Oracle 등 트랜잭션) = 고도화
- 테스트 데이터 심기 → 테스트 → 제거 트랜잭션 = 기본 플로우

---

## 6. DB 관련 회의 결정 종합 (회의록 1)

### 6.1 회의 출처

회의록 1 — "DB 관련 새로운 고려해야할 사항" (3 고려 + 4 수정).

### 6.2 3 고려사항

| # | 항목 | 본 docs 위치 | 책임 영역 |
|---|---|---|---|
| 1 | 테스트 데이터 생성 기준 및 저장 | §6.4 격차 | yujin #80 + B (시나리오) |
| 2 | DB 검증 결과 UI 표시 (위치 + 형태) | §6.4 격차 | 다른 팀 (UI) + E (CrossCheck 출력) |
| 3 | **선행 작업 (로그인 등) 뒷단 처리** | §6.4 격차 + §6.5 상세 | D (UITestTool) + B (시나리오 스키마) + C (ActionMapper) |

### 6.3 4 수정사항

| # | 항목 | 회의 결정 | 현 develop | 책임 |
|---|---|---|---|---|
| 1 | **DB 스캔 layer 0 (전처리)** | 테스트 데이터 생성 시 필요 | ❌ 현 pipeline DAG = L1A/L1B/L2/L3. **layer 0 개념 없음** | yujin #80 + A (pipeline DAG 노드 wire) |
| 2 | 테스트 데이터 준비 UI 관리 | 좀 더 생각 필요 | 미구현 | 다른 팀 (UI) + yujin |
| 3 | **스테이징 DB 접속 정보 받음** (서버에 동일 DB 띄우기 폐기) | 사용자 회사 staging DB 직접 접근 + 읽기/쓰기 권한 계정 + 테스트 후 롤백 | ⚠️ DBTestTool 가 `QAPILOT_MODULE_URL` 환경변수 의존 (단일 endpoint 가정). 사용자별 staging DB 접속 정보 받는 모델 미구현 | yujin #80 + DBTestTool 확장 (E 영역) |
| 4 | 실시간 DB 상태 UI | 진행 화면 / 결과 화면 / 둘 다? — 미정 | 미구현 | 다른 팀 (UI) |

### 6.4 격차 (회의록 1 기반)

| # | 격차 | 영역 | 우선순위 |
|---|---|---|---|
| **10** | DB 스캔 layer 0 신설 — pipeline DAG 의 새 노드 또는 L1A 한 노드로 통합 | A + yujin #80 | P1 (yujin #80 진척 의존) |
| **11** | DBTestTool 의 staging DB 접속 정보 모델 — `QAPILOT_MODULE_URL` 단일 → 사용자별 다중 (project_slug 단위 접속 정보 받아서 분기) | E (DBTestTool 본체) + A (pipeline 호출 인터페이스) | P0 (회의 결정 핵심) |
| **12** | UITestTool 선행 작업 (로그인 등) precondition 처리 메커니즘 — 현 UITestTool = step 단위만, TC 별 precondition 부재 | D (UITestTool) + B (시나리오 스키마 `precondition_steps` 신설) + C (ActionMapper 자동 prepend 옵션) | **P0** (외부 사용자 multi-TC 안정성 핵심) |
| **13** | 테스트 후 DB 롤백 정책 — 트랜잭션 vs 별도 cleanup vs 격리 schema. 회의 미정 | yujin #80 + E (DBTestTool) | P1 |
| **14** | 테스트 데이터 생성 기준 + 저장 모델 — RDB? 파일? scenarios.json 안? | B (시나리오) + yujin | P1 |
| **15** | DB 검증 결과 UI + 실시간 상태 UI | 다른 팀 (UI) | 별도 차수 |

### 6.5 UITestTool precondition (격차 12) 상세

**현 UITestTool 동작**:
- step 단위 실행만. TC 별 precondition (예: 로그인 상태 보장) 처리 부재
- 결과: 첫 TC 가 로그인 → 같은 browser context 의 cookie 가 다음 TC 로 누수 → TC 독립성 보장 X

**3 가지 해결 후보**:

| 옵션 | 영역 | 장점 | 단점 |
|---|---|---|---|
| (a) ActionMapper 가 precondition steps 자동 prepend | C (ActionMapper) | 한 TC self-contained | LLM 토큰 증가 + 반복 누적 (로그인 step × N TC) |
| (b) UITestTool 의 `precondition_steps` 별도 입력 | A+D 협업 | TC 본문 깔끔, precondition 분리 | scenarios.json 스키마 변경 (B 영역) |
| (c) TC 그룹별 로그인 상태 공유 + `browser.new_context()` 격리 | D (UITestTool) | 비용 효율 (로그인 1회) + 격리 보장 | TC 의존성 그래프 필요 (DAG group 개념 신설) |

**권장 안 — (c) + (b) 하이브리드**:
- 비싼 precondition (로그인 등) = TC 그룹 단위 공유 (옵션 c)
- 경량 precondition (cart 비우기 등) = scenarios.json 의 `precondition_steps` (옵션 b)

---

## 7. 다음 단계 제안 — 영역별 작업 분배

### 7.1 신규 협업 이슈 발의 후보

| 협업 이슈 | 관련 격차 | 책임 영역 |
|---|---|---|
| 외부 사용자 GitHub e2e entry point | 격차 1/2/3 통합 | C (projects_router + agent_router) + A 협업 |
| manifest RDB 전환 (Project 테이블 신설 + last_commit_hash 영속화) | 격차 8 (회의 결정 2 §4) | A (pipeline RDB 저장 layer) + C (models.py Project 테이블) |
| Git 이력 (blame/diff) RDB 캐싱 | 격차 9 (회의 결정 2 §2) | A + C + F (RootCauseAgent 로드 의존) |
| DBTestTool 의 staging DB 접속 정보 모델 | 격차 11 (회의록 1 §4-3) | E (DBTestTool) + A (pipeline 호출 인터페이스) + yujin #80 |
| UITestTool precondition (선행 작업) | 격차 12 (회의록 1 §3-3) | D (UITestTool) + B (시나리오 스키마) + C (ActionMapper) |
| DB 스캔 layer 0 (pipeline DAG 노드 신설) | 격차 10 (회의록 1 §4-1) | A + yujin #80 |

### 7.2 영역별 단독 작업 후보

| 영역 | 작업 |
|---|---|
| A | `_codebase_scan` 의 `cfg.project.repo_url` fallback (격차 4 — 격차 1/3 후) |
| D | UITestTool group context 격리 + cookie/session (격차 12 의 옵션 c 부분 — B/C 협업 전제) |
| C | projects_router 확장 (격차 2) + agent_router 의 cfg 로드 (격차 3) |
| E | DBTestTool staging DB 접속 정보 모델 (격차 11) |

### 7.3 회의 결정 후속 (별도 결정 필요)

| 항목 | 결정 필요 |
|---|---|
| GitHub token 정책 (격차 6) | (a) 우리 측 공용 (GitHub App / 서비스 계정) vs (b) 사용자별 PAT |
| Multi-repo framework/language 표현 (격차 7) | 현 `","join` 결합 vs repo 별 분리 vs primary 만 |
| DB 롤백 정책 (격차 13) | 트랜잭션 vs cleanup vs 격리 schema |
| 테스트 데이터 생성 + 저장 모델 (격차 14) | RDB vs 파일 vs scenarios.json 안 |

### 7.4 고도화 (별도 차수)

- ~~GitHub webhook 자동 trigger~~ → 회의 결정 폐기 (수동 트리거만)
- DB 권한 세부 관리 (Oracle 트랜잭션 등) — 회의 결정 = "권한 받아서 진행" 전제만, 세부 = 고도화
- 산업보안 강한 환경 대응 — 백업 플랜 (CLI 로컬 walk + 별도 서버)
- 실시간 DB 상태 UI / 테스트 데이터 준비 UI / DB 검증 결과 UI (회의록 1 의 UI 항목) — 다른 팀

---

## 8. 관련

### 회의록

- 교수님 피드백 회의 (2026-05-22) — 웹 방향 전환 + DB 관련 7 항목
- FR-000 코드베이스 스캔 Tool 수정 설계 결정사항 (2026-05-21, 6 항목)

### 관련 PR

- #146 GitCodebaseScannerTool 본체 (C)
- #153 파이프라인 연결 (C)
- #157 `_codebase_scan` 임시 로컬/Git 분기 (A) — **회의 결정 1 §1 정합** (기존 codebase_scanner_tool.py 유지)
- #162 GitCodebaseScannerTool 품질 fix (C)

### 관련 이슈

- #80 DB 스캔 모듈 (yujin) — 격차 10/11/13/14 연관
- #136 manifest 명명 + spec 정합 (A+C+docs) — 회의 결정 2 §4 (manifest RDB) 와 통합 검토
- #156 PR #157 원인
- 신규 이슈 후보: 격차 1/2/3 통합 / 격차 8 / 격차 9 / 격차 10 / 격차 11 / 격차 12
