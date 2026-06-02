# 도메인 규칙

{{domain_rules}}

# 분석 대상 문서

{{document_text}}

# 지시사항

위 도메인 규칙과 분석 대상 문서를 기반으로 테스트 가능한 모든 요구사항을 추출하라.

**[필수] 추출 절차:**
1. 먼저 문서 전체를 스캔하여 "FR-XXX-NN" 형식의 식별자(예: FR-AUTH-01, FR-ORDER-02, FR-CONTRACT-03)를 모두 나열하라.
2. 나열된 **모든 FR-XXX 식별자에 대해 반드시 별도의 REQ 항목을 1개씩 생성하라.** 절대 통합하거나 누락하지 마라.
3. content의 맨 앞에 "[FR-XXX-NN] " 형태로 원본 식별자를 포함하라 (예: "[FR-AUTH-01] 사용자는 회원가입할 수 있다").
4. FR-XXX가 없는 비기능 요구사항(성능·보안·가용성 등)도 추가로 추출하라.

**기타 규칙:**
- req_id는 문서에 있는 FR-XXX-NN 식별자 그대로 사용하라 (예: FR-AUTH-01, FR-ORDER-02). FR-XXX 없는 항목만 {{req_id_start}}부터 순번으로 부여하라.
- 각 요구사항의 우선순위(high/medium/low)와 도메인 영역을 반드시 명시하라.
- domain_area는 FR 식별자의 도메인 코드(AUTH/PLAN/ORDER/BNF/USG/BIL/NTC/PRF/CONTRACT/FAM/TIER)에 대응하는 한글 영역명을 사용하라 (예: AUTH→인증, ORDER→회선, CONTRACT→약정, FAM→가족, TIER→멤버십 등급).
- 도메인 규칙의 정책, 제한 조건, 예외 조건은 우선순위 결정에 반영하라.
- 문서에 근거가 없는 요구사항은 생성하지 마라.
- 지정된 JSON 형식으로만 출력하라. 설명이나 마크다운 없이 JSON만 반환하라.
