-- ════════════════════════════════════════════════════════════════════════
-- Migration 001 — metadata_indices 테이블 신설 (2026-06-09 토론 결정 4)
-- ════════════════════════════════════════════════════════════════════════
--
-- 목적:
--   기존 codebase_indices = SUT 구조 추출 (불변 fact, AST 결과)
--   신규 metadata_indices = TC/TV 생성 보조 (LLM 친화 schema 변환)
--   개념 분리 + sub_kind 자유 확장 + 기존 PR #240 의 mirror fallback 패턴 재사용
--
-- 영역 분류:
--   kind     | sub_kind   | 추출 방법       | confidence
--   ─────────┼────────────┼─────────────────┼──────────
--   frontend | selectors  | AST + LLM 의미  | 0.85~1.0
--   frontend | routes     | AST             | 1.0
--   backend  | schemas    | AST             | 1.0
--   sut_tests| patterns   | AST + LLM 분류  | 0.85~1.0
--
-- 본 DDL 은 PoC. 향후 alembic 도입 시 본 SQL 을 alembic revision 으로 전환.
-- ════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS metadata_indices (
    id              UUID         NOT NULL DEFAULT gen_random_uuid(),
    service_id      UUID         NOT NULL,
    commit_hash     VARCHAR(64)  NOT NULL,         -- 풀 git SHA (40자) 또는 file content sha256
    kind            VARCHAR(32)  NOT NULL,         -- "frontend" | "backend" | "sut_tests"
    sub_kind        VARCHAR(32)  NOT NULL,         -- "selectors" | "routes" | "schemas" | "patterns"
    s3_key          TEXT         NOT NULL,         -- services/{service_id}/metadata-index/{commit_sha}/{kind}-{sub_kind}.json
    bytes           BIGINT,                        -- S3 객체 크기
    sha256          VARCHAR(64),                   -- S3 객체 내용 sha256 (변경 감지)
    file_count      INTEGER,                       -- 추출 대상 파일 수
    confidence      NUMERIC(3, 2),                 -- 추출 신뢰도 (AST=1.0, LLM 의미라벨=0.85, LLM 추론=0.65)
    extraction_method VARCHAR(16),                 -- "ast" | "llm" | "hybrid"
    scanned_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (id),

    -- 같은 service + commit + kind/sub_kind 조합 = 한 record (재스캔 시 dedup)
    -- 재스캔 시 ON CONFLICT DO NOTHING 또는 명시적 UPDATE
    UNIQUE (service_id, commit_hash, kind, sub_kind),

    -- kind 유효성 (PoC — production 시 별도 lookup 테이블)
    CHECK (kind IN ('frontend', 'backend', 'sut_tests')),
    CHECK (sub_kind IN ('selectors', 'routes', 'schemas', 'patterns')),
    CHECK (extraction_method IN ('ast', 'llm', 'hybrid')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

-- 조회 패턴: service_id + kind/sub_kind 으로 최신 commit 검색
CREATE INDEX IF NOT EXISTS idx_metadata_indices_lookup
    ON metadata_indices (service_id, kind, sub_kind, scanned_at DESC);

-- 조회 패턴: commit_hash 직접 (변경 영향 추적)
CREATE INDEX IF NOT EXISTS idx_metadata_indices_commit
    ON metadata_indices (commit_hash);

COMMENT ON TABLE metadata_indices IS
    '2026-06-09 토론 결정 4 — TC/TV 생성 보조 메타데이터. codebase_indices 와 별도 namespace.';

COMMENT ON COLUMN metadata_indices.kind IS
    'frontend | backend | sut_tests — 추출 source 의 도메인';

COMMENT ON COLUMN metadata_indices.sub_kind IS
    'selectors | routes | schemas | patterns — 영역 (4 영역, docs/scan-enhancement/metadata-schema-spec.md)';

COMMENT ON COLUMN metadata_indices.s3_key IS
    'services/{service_id}/metadata-index/{commit_sha}/{kind}-{sub_kind}.json';

COMMENT ON COLUMN metadata_indices.confidence IS
    'AST 추출=1.0 / LLM 의미라벨=0.85 / LLM 추론=0.65. 유빈 agent 는 >=0.85 만 신뢰.';
