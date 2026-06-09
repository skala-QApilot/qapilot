# S3 Path / SHA / TTL / Lifecycle 규약

> 본 문서는 본 데이터 layer 의 S3 저장 규약을 정의한다. 코드베이스 원본 + 메타데이터 + 기존 codebase-index 의 path 통일.

---

## 0. 궁금증 정리

### Q1. 저장 방법 — RDB? 그래프 DB? NoSQL? 어떤 걸 쓰나?

**A. 하이브리드 — 본문은 S3 (object storage), "어디에 무엇이 있나" 의 카탈로그는 PostgreSQL (RDB).**

| Layer | 저장 위치 | 무엇을 저장 | 비유 |
|---|---|---|---|
| 본문 (blob) | **S3** (object storage) | `.py` `.vue` `.ts` 파일 텍스트 + 추출된 메타데이터 JSON | 책의 본문 |
| 인덱스 (catalog) | **PostgreSQL `metadata_indices` 테이블** | `s3_key`, `service_id`, `commit_hash`, `kind/sub_kind`, `bytes`, `sha256`, `confidence`, `scanned_at` | 도서관 카드 카탈로그 |

#### S3 - RDB 연계 구조
"어떤 service 의 최신 frontend.selectors 가 어디 있나?" 를 알려면 매번 `list_objects` 해야 함 (느림 + 비쌈). RDB 에 카탈로그가 있으면:
```sql
SELECT s3_key FROM metadata_indices
WHERE service_id=? AND kind='frontend' AND sub_kind='selectors'
ORDER BY scanned_at DESC LIMIT 1
```
한 쿼리로 끝. + 기존 PR #240 의 `_load_codebase_index_from_db_mirror()` 패턴 그대로 재사용 (검증된 패턴).

#### 그래프 DB 안 쓰는 이유
데이터는 "service × commit × 영역" 의 단순 다차원 lookup. joins 없음. 그래프 DB 는 callgraph 처럼 관계 탐색용 — 이미 `codebase_indices.callgraph` 가 JSON 으로 충분.

#### NoSQL document store (Mongo 등) 안 쓰는 이유
인덱스는 schema 가 고정 (kind, sub_kind, s3_key, ...). RDB 의 `CHECK` constraint + `UNIQUE` 제약이 더 안전. 기존 `codebase_indices` 와 정합.

---

### Q2. 다음 단계 (PoC 4+) 에서 어떻게 사용하나?

유빈 agent 가 TC/TV 생성 시 헬퍼 호출 → LLM context 주입:

```text
[유빈 TC generator]
   │
   │ ① load_metadata_index(service_id, "frontend", "selectors")
   ▼
[헬퍼: scan_storage.load_metadata_index]
   │ DB: SELECT s3_key FROM metadata_indices WHERE ... ORDER BY scanned_at DESC LIMIT 1
   │ S3: GET s3_key
   │ → dict (FrontendSelectorsIndex JSON)
   │
   │ ② load_source(service_id, sha, "backend/app/auth.py", line_start=10, line_end=50)
   ▼
[헬퍼: scan_storage.load_source]
   │ S3: GET services/{sid}/source/{sha}/backend/app/auth.py
   │ → line range 만 잘라서 반환 (token 절감)
   │
   ▼
[유빈 LLM prompt]
   """
   다음 selectors 카탈로그 사용해 TC 작성:
   {selectors_json}
   
   관련 코드:
   {auth_code_snippet}
   
   TS: {ts}
   """
```

**핵심 효과 (격차 매핑)**:
- **환각 selector 차단** — LLM 이 카탈로그 안의 testid (`name`, `email`, `signup-submit` ...) 만 사용
- **격차 A-1 (오라클) 완화** — schemas (Pydantic field) 로 "이메일 검증 룰" 확인 가능
- **격차 A-3 (런타임 상태)** — TV pool 도 같은 load_* 패턴 (PoC 6)

PoC 2 의 `FrontendSelectorsIndex` 가 바로 이 prompt context 의 입력.

---

### Q3. `git clone --depth 1` 의 `depth 1` 의미?

**shallow clone — git history 1 commit (HEAD) 만 가져오기.**

| | `git clone` (full) | `git clone --depth 1` |
|---|---|---|
| 가져오는 것 | 전체 history (모든 commit) | 마지막 commit (HEAD) 만 |
| `.git` 크기 | service 누적 수백 MB ~ GB | ~수 MB |
| 속도 | 느림 | 10~100배 빠름 |
| 과거 commit | `git log` 전체 + `git checkout <old_sha>` 가능 | 못 봄 |
| 사용처 | 개발 | CI / 1회 스캔 |

#### PoC 에서 쓰는 이유
- **현재 코드만** 필요 — 과거 history 무관. commit 추적은 `metadata_indices.commit_hash` 컬럼만 있으면 충분.
- 1회 clone → 스캔 → S3 PUT → 즉시 폐기 (workspace) → 디스크 자국 0.
- 대용량 service repo 의 history 가 GB 일 때, depth 1 = 코드 자체 크기만 (예: 50MB).

#### Trade-off
- 단점: 과거 commit 접근 불가 — 단 service 등록 시점 HEAD 만 필요하므로 무관.
- repeat scan = 새 SHA 마다 다시 `git clone --depth 1` (캐시는 S3 의 `head_object` skip 으로).

---

## 1. S3 Bucket / Prefix 종합

```
qapilot-local (bucket — PoC, production 은 별도)
└── services/
    └── {service_id}/                                # UUID
        ├── codebase-index/                          # 기존 (변경 X)
        │   └── {commit_sha}/                        # 짧은 hash (12자)
        │       ├── endpoints.json
        │       ├── models.json
        │       ├── functions.json
        │       ├── callgraph.json
        │       ├── manifest.json
        │       └── frontend.json
        ├── metadata-index/                          # 신규 (본 작업으로 추가)
        │   └── {commit_sha}/
        │       ├── frontend-selectors.json
        │       ├── frontend-routes.json
        │       ├── backend-schemas.json
        │       └── sut_tests-patterns.json
        ├── source/                                  # 신규 — 코드베이스 원본
        │   └── {commit_sha}/
        │       └── {relative_path}                  # ex: backend/app/main.py
        ├── generated-code/                          # 기존 (변경 X)
        │   └── {tc_id}/
        │       └── v{n}.js
        ├── domain/                                  # 기존 (변경 X)
        │   └── ...
        └── results/                                 # 기존 (변경 X)
            └── {trace_id}/
                └── ...
```

---

## 2. 신규 path 상세

### 2.1 metadata-index
```
services/{service_id}/metadata-index/{commit_sha}/{kind}-{sub_kind}.json
예: services/9f2a7d4f-.../metadata-index/76ff013448.../frontend-selectors.json
```

- `commit_sha` = 풀 SHA (40자) 또는 짧은 hash (12자, 기존 패턴과 정합)
- 파일명 = `{kind}-{sub_kind}.json` — 한 파일 = 한 record
- `kind` ∈ `{frontend, backend, sut_tests}`
- `sub_kind` ∈ `{selectors, routes, schemas, patterns}`

### 2.2 source (코드베이스 원본)
```
services/{service_id}/source/{commit_sha}/{relative_path}
예: services/9f2a7d4f-.../source/76ff013448.../backend/app/main.py
```

- `relative_path` = repo root 기준 상대 경로
- 파일 그대로 PUT (text/plain MIME) — 원본 보존
- 단 binary 파일 (이미지/PDF 등) 은 스캔 제외 (extension whitelist)

#### Whitelist (PoC)
- 텍스트: `.py .js .ts .tsx .jsx .vue .html .css .scss .json .yaml .yml .md .toml .sql`
- 단 .lock/.lock.json 제외 (큰 파일 + 불필요)
- 단 node_modules/, dist/, build/, .git/, .venv/, __pycache__/ 제외

#### File count 추정 (PoC PUT 비용)
- mini-bss-lite = ~150 files (frontend + backend)
- 평균 PUT = $0.005/1000 → 150 file = **$0.00075** (무시 가능)
- 1000 service 누적 = 15만 PUT = **$0.75** (PoC 단계 무시 가능)

---

## 3. SHA 결정성 정책

### 3.1 SHA 의 출처
- **Git commit SHA** 우선 (실제 코드 버전 추적)
- repo HEAD 기준 + 변경 없으면 그대로
- repo 가 없는 경우 (local upload 등) = 파일 내용 sha256

### 3.2 Cache 정책
- 같은 (service_id, commit_sha) 의 metadata-index / source 이미 S3 에 있으면 **PUT skip**
- `head_object` 로 확인 (list_objects 대신 — 빠름)
- 기존 PR #240 의 mirror fallback 패턴 그대로 재사용

### 3.3 짧은 hash vs 풀 SHA
- 기존 `codebase_indices` 의 `commit_hash` = `character varying` = 풀 SHA (길이 제한 없음)
- 신규 `metadata_indices` = 동일하게 `character varying`
- S3 path 의 `{commit_sha}` = **짧은 12자** 사용 (path 길이 절감 + 기존 패턴과 정합)
  - 단 DB 의 commit_hash 는 풀 SHA 저장 (lookup 시 짧은 hash 로 LIKE 검색)

---

## 4. TTL / Lifecycle

### 4.1 S3 Lifecycle Policy (PoC)
```yaml
# bucket: qapilot-local
rules:
  - id: source-30day-cleanup
    filter: {prefix: "services/*/source/*"}
    actions:
      - expire_after_days: 30  # 마지막 GET 으로부터 30일
  - id: metadata-keep
    filter: {prefix: "services/*/metadata-index/*"}
    actions: []  # 영구 보존 (DB 의 commit_hash 와 정합 유지)
```

### 4.2 Service 삭제 시 cascading
- service_id 삭제 시 `services/{service_id}/` 하위 전부 삭제 (lifecycle 즉시)
- DB 의 `metadata_indices.service_id` FK CASCADE

### 4.3 Repeat scan
```
service A 의 commit SHA 가 76ff... 으로 동일:
  1. head_object("services/A/metadata-index/76ff.../frontend-selectors.json")
  2. 존재 → skip (재사용)
  3. 미존재 → 추출 + PUT
```

---

## 5. 보안 (IAM 분리는 폐기)

### 5.1 PoC 단계 결정
- 단일 MinIO instance + 단일 credentials (`minioadmin/minioadmin`)
- service 간 격리 = **path prefix 만** (`services/{service_id}/...`)
- 누적 PR (mirror_writer 등) 도 동일 패턴

### 5.2 향후 production 시점 (별도 검토 필요)
- multi-tenant 시 service A 의 owner 가 service B 코드 접근 막아야
- AWS S3 IAM Policy 분리 (`Resource: arn:aws:s3:::qapilot-prod/services/{service_id}/*`)
- #257 처럼 별도 이슈 발의

### 5.3 GitHub 인증 (별개 layer)
- service 등록 시 `repos[].token` (GitHub PAT) = **GitHub 접근** 만
- MinIO/S3 와 무관
- 한 번 PAT 으로 `git clone --depth 1` → S3 PUT → 폐기 → 이후 S3 접근만 (PAT 재사용 X)

---

## 6. 조회 인터페이스 (헬퍼 시그니처)

### 6.1 source 조회
```python
def load_source(
    service_id: str,
    commit_sha: str,
    path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    """S3 의 source/{sha}/{path} 에서 본문 로드.
    
    line_range 가 주어지면 해당 라인만 잘라서 반환 (token 절감).
    line_range 없으면 전체 본문.
    
    캐시 정책:
    - 메모리 LRU (per-process, 100MB cap)
    - 같은 (service_id, sha, path) 호출 = cache hit
    """
```

### 6.2 metadata-index 조회
```python
def load_metadata_index(
    service_id: str,
    kind: str,        # "frontend" | "backend" | "sut_tests"
    sub_kind: str,    # "selectors" | "routes" | "schemas" | "patterns"
    commit_hash: str | None = None,  # None 시 최신
) -> dict:
    """metadata_indices DB → S3 mirror fallback.
    
    기존 PR #240 mirror fallback 패턴 그대로:
    1. 디스크 cache (선택)
    2. DB 의 s3_key 확인
    3. S3 GET
    4. JSON parse + 반환
    """
```

### 6.3 코드베이스-index 조회 (기존 — 변경 X)
- `qapilot/db/code_reader.load_codebase_index(service_id, kind)` 그대로

---

## 7. 비용 모니터링 (PoC → production)

### 7.1 측정 항목
- S3 PUT 횟수 (월간) — `bytes/file × file_count × service_count`
- S3 storage (월간 누적)
- S3 GET 횟수 (월간) — TC/TV 생성 시 호출
- GitHub API rate (대체 시 비교)

### 7.2 비용 threshold
- **PoC 단계 (< $10/월)**: 무관, 결과물 퀄리티 우선
- **MVP 단계 ($10~$100/월)**: 캐시 hit rate 모니터링 + lifecycle 30일 → 14일 단축 검토
- **Production 단계 (> $100/월)**: GitHub MCP on-demand 부분 전환 + source/ 영구 보존 폐기 (metadata-index 만 유지)

---

## 8. 본 작업 우선순위

1. **PoC 1** (본 단위): docs + Pydantic model + DDL ← 현재 작업
2. **PoC 3**: S3 writer (`upsert_metadata_index()`) + 기존 mirror 패턴
3. **PoC 4**: S3 reader (`load_metadata_index()` + `load_source()`)

source/ 디렉토리 저장은 PoC 2 (AST 추출) 와 같이 진행 — AST 추출 후 원본도 PUT.
