# 웹 방향 전환 + GitHub e2e 현황 갭 분석 (2026-05-22)

> **목적**: 2026-05-22 교수님 피드백 회의에서 결정된 *"완전 웹 + GitHub 연동 e2e"* 방향이 현재 develop 코드에서 실제로 가동 가능한지 정량 점검. Tool 본체 / Pipeline 분기 / Entry point 3-tier 분석 + 격차 + 백업 플랜 + 본인 영역 영향 정리.
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

### 3.1 Tier 1 — Tool 본체 (완비 ✅)

| 항목 | 위치 | 상태 |
|---|---|---|
| **GitCodebaseScannerTool** | `qapilot/tools/git_codebase_scanner_tool.py` | ✅ PR #146 + #162 |
| GitHub/GitLab 어댑터 (`GitPlatformAdapter` ABC) | line 120/164/243 | ✅ 다중 플랫폼 |
| Bearer token 인증 | line 174 | ✅ |
| tree-sitter 파싱 (py/js/ts/java/yaml) | line 26-30 | ✅ |
| Multi-repo (`params["repos"]`) — MSA 지원 | line 484-509 | ✅ |
| 증분 스캔 (`last_commit_hash` + HEAD~1 폴백) | line 417 + PR #162 | ✅ |
| `scan_status` 메타 (`full/incremental`) | line 461-465 (PR #162) | ✅ |
| **CodebaseScannerTool** (로컬 walk) | `qapilot/tools/codebase_scanner_tool.py` | ✅ — 본인 PR #157 임시 fallback |

### 3.2 Tier 2 — Pipeline 분기 (완비 ✅, 단 입력 의존)

| 항목 | 위치 | 상태 |
|---|---|---|
| `_codebase_scan` 분기 (본인 PR #157) | `qapilot/orchestrator/pipeline.py:267-297` | ✅ |
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
| `token` | **TBD** | ⚠️ **미정** | `params.get("token", "")` — **사용자별 입력 가정. default 빈 문자열 = public repo unauthenticated (GitHub rate limit 60/시간)**. "우리 측 공용" 정책 미구현 | (a) 우리 측 공용 (GitHub App / 서비스 계정) — secret store + `_normalize_repos` fallback layer 신설 필요 vs (b) 사용자별 PAT 입력 — projects/register 필드 + qapilot.config.yaml 저장 (보안 검토). **정책 결정 후 코드 정합**. PR #146/#162 본문에도 정책 명시 없음 |
| `framework` | Central Hub | ❌ 입력 X | ✅ `_detect_language_framework()` 휴리스틱 완비 (pom.xml→spring / pyproject.toml→fastapi·django·flask / package.json→react·vue·next 등). **multi-repo 시 `","join(...)` 결합 형태** (`"fastapi,react"`) | GitCodebaseScannerTool 이 repo 종합 분석으로 자동 추론. ProjectRecord 의 framework 가 단일 string 이면 **multi-repo 표현 정책 결정 필요** |
| `language` | Central Hub | ❌ 입력 X | ✅ 동일 (`_LANG_MAP` 카운트 top language) | 동일. multi-repo 콤마 결합 |

### 3.4 실 e2e 검증 결과

```
GitCodebaseScannerTool 발동 경로 (현 develop):

1. CLI `qapilot generate scenarios` → ❌ Git 모드 진입 불가 (옵션 없음)
2. Web API /api/agent/scenario-generation → ❌ Git 모드 진입 불가 (body 추출 안 함)
3. 단위 테스트 (test_git_codebase_scanner_tool.py) → ✅ 53 passed (PR #162)
4. 본인 단위 테스트 (test_pipeline_codebase_scan_fallback_156.py) → ✅ 4 passed
   - Git 모드 진입은 mock 으로만 검증 (run_options.repo_url 주입)

⚠️ 실 e2e 에서 GitCodebaseScannerTool 가 한 번도 호출된 적 없음 (단위 테스트만).
```

---

## 4. 격차 — "추후 완전 웹 + GitHub e2e" 진입 전제 조건

### 4.1 격차 표

| # | 격차 | 영역 | 본인 단독 | 우선순위 |
|---|---|---|---|---|
| **1** | `ProjectConfig` 에 `repo_url/branch` 필드 추가 (**token 정책 결정 후 필드 형태 확정**) | A+C 공유 (`shared/config.py`) | 부분 | P1 |
| **2** | `ProjectRecord` + `/api/projects/register` 가 외부 사용자 입력 수용 + 서버 측 qapilot.config.yaml 자동 생성. **단 사용자 입력 = `repo_url/branch/target_url` 만. `framework/language` 는 Central Hub 가 GitCodebaseScannerTool 출력으로 자동 채움** | C 영역 (`api/projects_router.py` + `shared/project_registry.py`) | ❌ | P0 |
| **3** | `/api/agent/*` 엔드포인트가 qapilot.config.yaml 로드 → `RunOptions` 자동 채움 (또는 pipeline 노드가 cfg 에서 직접 읽기) | A+C 공유 (`api/agent_router.py` + `orchestrator/pipeline.py`) | 부분 | P0 |
| **4** | 본인 `_codebase_scan` 분기 — `cfg.project.repo_url` fallback 추가 (이중 출처: RunOptions 우선 + cfg fallback) | A 본인 | ✅ | P1 (격차 1/3 후) |
| **5** (인프라) | GitHub webhook 같은 자동 trigger | DevOps | ❌ | 고도화 |
| **6** (정책 미정) | **token 정책 결정** — (a) Central Hub 공용 token (GitHub App / 서비스 계정, secret store + `_normalize_repos` fallback layer 신설) vs (b) 사용자별 PAT 입력 (projects/register 필드 + qapilot.config.yaml 저장 + 보안 검토). **현 코드 = (b) 사용자별 가정으로 작성**. 결정 후 격차 1/2 의 token 처리 방식 확정 | 회의 결정 | ❌ | P0 (격차 1/2 선행) |
| **7** (정책 미정) | **multi-repo framework/language 표현 정책** — GitCodebaseScannerTool 이 multi-repo 시 `","join(...)` 결합 형태 출력 (`"fastapi,react"`). ProjectRecord 가 단일 string 필드면 (a) 콤마 그대로 저장 vs (b) repo 별 분리 필드 vs (c) primary framework 만 추출. **현 코드 = (a) 콤마 결합 그대로** | 회의 결정 | ❌ | P1 (격차 2 동반) |

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
│ 3. 본인 _codebase_scan → is_git_mode=False (repo_url 없음)        │
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
| 개발자 로컬 e2e (mini-bss-lite) | CLI + `CodebaseScannerTool` (PR #157 로컬 fallback) | ✅ — 본인 트랙 |

### 5.3 DB 권한 전제

- **기본 전제**: *"DB 접속 권한을 받아서 진행합니다"*
- 세부 권한 관리 (Oracle 등 트랜잭션) = 고도화
- 테스트 데이터 심기 → 테스트 → 제거 트랜잭션 = 기본 플로우

---

## 6. 본인 영역 (A+D) 영향 — 정확 정합

### 6.1 작업 우선순위 재정의

| 작업 | ❌ 이전 가정 | ✅ 정정 |
|---|---|---|
| CLI 위치 | 격하 또는 사라짐 | **개발자 보조 진입점 — 유지** (격하 X). 외부 사용자 = 웹 UI |
| #135 init 자동화 | 외부 사용자 1st-run UX (P0) | **개발자 로컬 e2e 1st-run UX 로 재정의**. 외부 사용자 onboarding 은 웹 UI (다른 팀) |
| #37 REPL | "보류" 격하 | **개발자 dx — 유지** (외부 사용자 무관) |
| #108/#109 CLI UX | 우선순위 ⬇ | **개발자 보조 — 유지** |
| PR #157 임시 분기 | "회의 후 제거 확정" | **외부 사용자 GitHub e2e 정착 후 부분 제거** (로컬 walk 는 백업 플랜 보존) |
| `_codebase_scan` 분기 정합 | 받을 준비 ✅ | **`cfg.project.repo_url` fallback 추가** (격차 4 — 본인 단독, 격차 1/3 후) |

### 6.2 시한폭탄

| 폭탄 | 시한 |
|---|---|
| PR #157 임시 로컬 분기 | 외부 사용자 GitHub e2e 정착 시점. 단 **로컬 walk 는 백업 플랜으로 보존** — 완전 제거 X |
| 격차 4 (`_codebase_scan` 의 cfg fallback) | 격차 1/3 (C 영역) 진척에 의존 |
| Web e2e 진입의 실제 검증 (격차 1~4 모두 해소 후) | C 영역 작업 시점 |

### 6.3 본인 A 영역 작업 — 신규 후보

1. **격차 4 정합 작업** — `_codebase_scan` 의 cfg.project.repo_url fallback 추가 (~1시간)
   - 단, 격차 1 (`ProjectConfig` 에 repo_url 필드 추가) 선행 필요
2. **C 영역 협업 이슈 발의** — 격차 1/2/3 통합 이슈 등록 (cross-area 협업)
3. **본인 메모리 + spec 정합 갱신** — 본 문서 반영
4. **mini-bss-lite e2e 보존** — 로컬 walk 백업 플랜 검증 (개발자 트랙)

### 6.4 깊은 의미 — 본인 메모리 사례 반복 학습

본 docs 작성 과정에서 **본인 메모리 사례 #1/#6/#8 패턴 3건 잠재 발견**:

| 사례 | 본 docs 의 검증 |
|---|---|
| **#1 (mock 함정)** | `test_pipeline_codebase_scan_fallback_156.py` 의 Git 모드 mock 검증과 실 e2e 호출 경로 간극 잠복 (CLI/Web API 가 repo_url 안 채움). 단위 테스트만 통과 |
| **#6 (이슈 중복)** | 본 docs 의 격차 표가 기존 OPEN 이슈 (#136 §A manifest / #156 분기 / 신규 #?) 와 중복 검증 필요 |
| **#8 (산출물 vs 의미)** | 회의 직후 본인 1차 *"CLI 격하"* 오해 → 2차 *"target_url RunOptions 매번 넘김"* 오해 → 사용자 정정 |

행동 수칙 #5 ("메모리 ↔ 코드 cross-check") + #8 ("산출물 vs 의미 분리") 실천 사례로 본 docs 자체가 학습 자료.

---

## 7. 다음 단계 제안

### 7.1 본인 단독 (즉시 가능)

- 본 docs 머지 (메모리 갱신 + spec 정합 일부)
- mini-bss-lite 로컬 e2e 보존 검증

### 7.2 협업 이슈 발의 (C 영역)

- **신규 이슈**: 외부 사용자 GitHub e2e entry point — projects/register + ProjectConfig 확장 + agent_router 의 cfg 로드 정합
- 본 docs 의 격차 1/2/3 통합. 영역: C 본질 + A 협업

### 7.3 본인 격차 4 (격차 1/3 진척 후)

- `_codebase_scan` 의 `cfg.project.repo_url` fallback 추가
- 단위 테스트 + e2e 검증

### 7.4 고도화 (별도 차수)

- GitHub webhook 자동 trigger
- DB 권한 세부 관리
- 산업보안 강한 환경 대응

---

## 8. 관련

- 본인 메모리: `project_meeting_decision_2026_05_22_web_direction.md` (신규)
- 본인 메모리: `project_qapilot_my_role_A.md` (갱신)
- 본인 메모리: `project_qapilot_deployment_topology.md` (갱신)
- 본인 메모리: `project_qapilot_npt_stages_and_roadmap.md` (Stage 4 정의 갱신)
- 본인 메모리: `feedback_deep_semantic_review.md` (사례 #1/#6/#8 학습 사례 갱신)
- 회의록: 교수님 피드백 회의 (2026-05-22)
- 관련 PR: #146 (GitCodebaseScannerTool 본체) / #153 (파이프라인 연결) / #157 (본인 임시 분기) / #162 (품질 fix)
- 관련 이슈: #136 (manifest 명명 + spec 정합) / #156 (PR #157 의 원인) / 신규 이슈 (격차 1~3 협업)
