# QApilot 성능 측정 설계서 — 테스트 코드 정확도 · 유효 판정율 · 장애 분류/원인 추론 정확도

> 담당: 김주환 · 브랜치 `feat/juhwan/metrics-measurement`
> 근거: 실제 코드(파이프라인·cross_check·_classify_failure·root_cause·defect_writer) 전수 조사 + golden/faults ground truth 검증 + 학술/상용 벤치마크 리서치.
> 목적: 그림(개발표준정의서 §15.3 / WBS KPI)의 미측정 지표를 **재현 가능하고 근거 있는** 방식으로 측정.

---

## 0. 측정 대상 지표 (담당 범위)

| 지표 | 한 줄 정의 | 성격 |
|---|---|---|
| **유효 판정율** | 시스템이 유의미한 결론(PASS/결함검출FAIL)을 낸 비율 | 완전성 (coverage) |
| **테스트 코드 정확도** | 생성된 테스트가 SUT를 의도대로 실행해 **오라클과 일치하는 verdict**를 내는 비율 | 정확성 (correctness) |
| **결함 검출 정확도** | 주입 결함(fault)을 검출(FAIL)하는 Recall/Precision | 정확성 |
| **장애 분류 정확도** | 검출 결함의 분류(①장애유형 + ②결정분류)가 정답과 일치하는 비율 | 정확성 |
| **원인 추론 정확도** | 추론된 root_cause가 정답과 일치하는지 (Top-N + 의미) | 정확성 |

핵심 원칙: **완전성(유효 판정율)과 정확성(나머지)은 직교한다.** 결론을 냈다고(완전) 그 결론이 맞는 건(정확) 아니다.

---

## 1. Ground truth (정답지) — 두 출처, 서로 다른 용도

| 출처 | 정체 | 무엇의 정답 |
|---|---|---|
| `mini-bss-lite/golden/` | 정답 **시나리오** 114케이스 (green backend test 유래, 오라클=실제 정상 동작) | 시나리오 생성 정확도 + **테스트 코드 정확도(정상 SUT에서 올바른 verdict)** |
| `mini-bss-lite/faults/` | 주입 **결함** 7개 (BUG-*.yaml: affected_scenarios·category·root_cause·fix) | **결함 검출 / 장애 분류 / 원인 추론** |

### 1.1 Fault ground truth 신뢰성 검증 결과 (선행 필수)

7개 fault를 코드↔yaml↔정책문서 전수 검증한 결과 — 3개가 수정 전 사용 불가였고, 그중 측정에 곧장 필요한 2개(RULE-003·seed)는 **본 브랜치에서 수정 완료**:

| Fault | 종합 | 이슈 / 조치 |
|---|---|---|
| BUG-RULE-001 (미성년 동의X 가입) | ✅ 사용가능 | — |
| BUG-RULE-002 (미성년+일반요금제) | ✅ 사용가능 | trigger = 미성년+YOUTH_SAFE 보유 선행 셋업. seed에 미성년+보호자동의 계정 추가로 해소 (`minor@minibss.test`) |
| BUG-RULE-004 (성인+청소년요금제) | ✅ 사용가능 | ~~seed birth_date=None → 검증 우회~~ → **수정**: seed 성인 계정에 `birth_date` 부여 (커밋 7f07cdf) |
| BUG-API-001 (중복약정 409→500) | ✅ 사용가능 | 문서 조항 매핑만 느슨 |
| BUG-DATA-001 (날짜 UTC/KST) | ⚠️ 측정 제외 | start_date 명시 시 무력화 + 시간대 비결정 → 초기 측정 신뢰 subset에서 제외 |
| BUG-INFRA-001 (배치 race) | ⚠️ 측정 제외 | asyncio 단일루프 race 비보장 → 초기 측정 신뢰 subset에서 제외 |
| BUG-RULE-003 (상위변경 위약금) | ✅ 사용가능 | ~~status=TERMINATED 후 penalty 계산 → 항상 0, 검출 불가~~ → **수정**: penalty를 상태변경 전(ACTIVE)에 계산 (커밋 7f07cdf) |

→ **신뢰 측정 subset = RULE-001/002/003/004 + API-001 (5종)**. DATA-001·INFRA-001은 결정성 확보 후 편입(별도 후속).
→ 선행 수정 없이는 해당 fault에 대한 "검출 실패"가 QApilot 탓이 아니라 정답지 탓이 되어 측정이 왜곡되므로, 위 수정이 측정의 전제다.

---

## 2. 측정 환경 — 전용 측정 SUT (배포 인스턴스와 분리)

배포 SUT(minibss-team3)는 ① clean(fault OFF) ② env 변경 불가 ③ 낡은 seed(birth_date=None) 라서 **측정 대상으로 부적합**. 측정은 별도 인스턴스에서:

- **GitHub 소스로 빌드한 전용 SUT** (로컬 docker-compose 또는 임시 배포)
- 구성별 **`ENABLED_FAULTS` 토글** (clean / 각 fault ON)
- **fault-triggering seed** (성인·미성년 계정 모두 `birth_date` 보유)
- QApilot은 이 인스턴스를 가리키는 **별도 service 등록** (target/staging URL)
- 배포 공유 SUT는 라이브 시연용으로 분리 (측정과 무관)

**SUT 상태 매트릭스**: `clean(1) + fault-ON(신뢰가능 fault 수)` = 각 구성을 측정 단위로 사용.

---

## 3. 지표별 측정 방법 (실제 코드 정합)

파이프라인: `Layer1(scenario→action_map→code) → Layer2(UI/API/DB Tool→CrossCheck=verdict P/F/S/U) → Layer3(_classify_failure 결정분류 → PRODUCT_DEFECT_CANDIDATE만 LLM root_cause Top-N → fix → defect)`

### 3.1 유효 판정율 (이미 코드 정의됨)
>테스트가 "맞다/틀리다"를 분명히 판단한 비율입니다. 판단을 보류(U)하거나 검증을 못 끝낸(S) 경우는 빼서, 시스템이 얼마나 "결론까지 갔는지"(완전성)를 봅니다.
```
유효 판정율 = (PASS + 결함검출 FAIL) / 전체 TC          # TestResultPage.tsx:410
            = 시스템이 유의미한 결론을 낸 비율 (S=검증미완, U=판정보류 제외)
```
- 자동 산출 (서버 ResultResponse: pass/fail/skip/unverified count). 별도 정답지 불필요.
- **의미**: "얼마나 결론을 냈나"(완전성). 정확성과 분리.

### 3.2 테스트 코드 정확도 — golden 오라클 대조 (clean SUT)
>결함 없는 정상 SUT에 돌렸을 때, 정답지(golden)가 "통과"라 한 케이스를 실제로 통과시키는지 봅니다. 생성된 테스트가 엉뚱한 요소를 누르거나 잘못 검증하면 여기서 틀립니다 — 즉 "결론을 냈는가"가 아니라 "그 결론이 맞는가"(정확성)를 봅니다.
```
테스트 코드 정확도 = (verdict가 golden 오라클과 일치한 TC) / (전체 TC)   # clean SUT 기준
```
- **clean SUT**(fault OFF): golden은 전 케이스 통과 기대(정상=PASS, negative=올바른 거부도 PASS).
- false-fail(셀렉터/매핑 오류 → `TEST_DEFECT_MAPPING`/`UNVERIFIABLE`로 분류됨) = 테스트 코드 부정확.
- `_classify_failure`의 TEST_DEFECT 계열이 **테스트 측 결함을 시스템이 자가 식별** → 보조 신호로 활용, 단 **객관 척도는 golden 오라클 대조**.
- **Q1 답: 유효 판정율 ≠ 테스트 코드 정확도. 독립 측정.** (완전성 vs 정확성)

### 3.3 결함 검출 정확도 — mutation-testing 방식
>일부러 심은 버그(fault)를 켰을 때 테스트가 그걸 잡아내(FAIL)는 비율입니다. 소프트웨어 테스트 업계의 검증된 방식인 "mutation testing(심은 버그를 죽이는 비율)"과 같으며, 상용 도구(Diffblue)도 결함 검출력 지표로 씁니다.
fault X ON으로 실행 → affected TC가 `PRODUCT_DEFECT_CANDIDATE`로 FAIL하나:
```
Recall(검출율)   = 검출된 fault / 주입 fault              # = mutation kill rate
Precision        = 진짜 결함검출 FAIL / 전체 결함검출 FAIL  # clean SUT의 false-positive로 보정
```
- 학술/상용(Diffblue)의 "seeded bug kill rate"와 동일 방법론 → 방어 가능.

### 3.4 장애 분류 정확도 — ①장애유형 + ②결정분류 종합
>잡아낸 결함을 "무슨 종류(요금제 규칙/API/데이터…)"이자 "누구 책임(제품/테스트/환경)"인지 둘 다 맞게 분류하는지 봅니다. 정답은 심어둔 버그 정의(faults)에 적혀 있습니다.
정답: faults/ `category`(RULE→DOMAIN_RULE, API→API_ERROR, DATA→DATA_MISMATCH, INFRA→INFRA) = **①장애유형**.
- ✅ **확장 완료**: 기존엔 제품 결함에 ②결정분류(PRODUCT_DEFECT_CANDIDATE)만 저장하고 ①은 소실됐으나, `defects.defect_type` 컬럼(qapilot-server V19) + `defect_writer._infer_defect_type`(qapilot)로 ①장애유형을 ②와 별도 산출·저장하도록 확장. 단서 없으면 None(임의 기본값 미사용).
- 측정: 검출된 결함에 대해
  - ②결정분류 정확도: PRODUCT_DEFECT_CANDIDATE로 옳게 분류했나 (TEST/ENV 오분류 아닌가)
  - ①장애유형 정확도: faults/ category와 일치하나 (혼동행렬)
  - **종합 = ① ∧ ② 둘 다 맞은 비율**

### 3.5 원인 추론 정확도 — Top-N + 의미 2단계
>추론한 원인이 정답 위치(파일·함수)와 맞는지를 "상위 N개 후보 안에 들었나(Top-N)"로 봅니다(개발자 대부분이 상위 5개만 확인). 설명이 의미상 맞는지는 AI 심판이 채점하되, 일부는 사람이 검수해 신뢰도를 보정합니다.
시스템은 `PRODUCT_DEFECT_CANDIDATE`에 대해 **root_cause 후보 Top-N**(file_path·cause·confidence·evidences) 산출. 정답 = faults/ `root_cause`(file·function·조건·약관조항).

- **(a) 위치 일치 — 자동, Top-N 지표**: 정답 file(+function)이 시스템 후보 Top-1/Top-3/Top-5에 포함되나 → **Top-N accuracy + MRR**(Mean Reciprocal Rank). (FL 표준; 개발자 73%가 Top-5만 확인)
- **(b) 의미 일치 — LLM-judge**: 후보 `cause` 설명이 정답 root_cause의 "왜"(약관 위반·우회 조건)와 의미적으로 일치하나 → LLM 심판이 0/1 채점.
- **사람 spot-check**: LLM-judge 결과의 ~20% 표본을 사람이 검수해 심판 신뢰도 보정.
- 최종 원인 정확도 = **Top-N 위치 정확도 + 의미 일치율**(LLM-judge, spot-check 보정).

### 3.6 conditional vs end-to-end (둘 다 보고)
파이프라인이 PRODUCT만 root_cause로 게이팅하므로:
- **단계별 conditional** (주력): 분류 정확도(검출된 결함 한정), 원인 정확도(검출+PRODUCT 한정) → 에이전트 자체 품질.
- **end-to-end 합성** (보조): 검출 × 분류 × 원인 → 사용자 체감 정확도.

---

## 4. 비결정성 처리 (LLM)

- **반복 N**: 구성당 **3회 기본**(사용자 결정), 통계 안정성 위해 **5회 권장**(업계 judge-agent 관행).
- **지표화**:
  - 검출/분류: **pass^k(일관성)** 우선 — QA 도구는 "가끔 검출"보다 "항상 검출"이 중요. 함께 **mean±std** 보고.
  - 원인 Top-N: N회 평균 + std.
- seed 고정(가능 범위) + cache로 결정성 보강(기존 metadata 캐시 활용).
- ⚠️ 단일 SUT 한계: 반복은 표본 확대가 아님(같은 SUT). 일반화 주장은 보류, "본 SUT 기준" 명시.

---

## 5. api-mode vs ui-mode — 통합 측정

- api-mode는 UI 오라클 한계(서술형 assert 등)를 API/DB로 **보완하는 cross-check 축** → 분리 측정하면 보완 효과를 못 봄. **통합 측정이 정답**.
- 단 진단용 모드별 breakdown은 부가 지표로 병기.

---

## 6. 목표/최소 기준 (벤치마크 근거)

| 지표 | 최소 | 목표 | 근거 |
|---|---|---|---|
| 테스트 코드 정확도 | 80% | 90%+ | Diffblue 80.7% coverage / 베이스라인 2.5배 |
| 결함 검출(Recall) | 70% | 85%+ | mutation kill rate 상용 수준 |
| 장애 분류(①∧②) | 70% | 85%+ | 결정분류는 결정적이라 高 기대 |
| 원인 추론 Top-5 | 60% | 75%+ | LLM fault localization 연구 Top-5 ~50-70% 상회 목표 |
| 유효 판정율 | 85% | 95%+ | 현 시스템 cross-check 보완 반영 |

(목표치는 측정 1차 결과 후 재보정 — 우선 "시중 상용 대비 우위" 지향.)

---

## 7. 선행 작업 (착수 순서)

1. ✅ **SUT fault 수정** (mini-bss-lite, 커밋 7f07cdf): RULE-003 penalty 순서 + seed birth_date/미성년 계정. DATA-001/INFRA-001은 측정 제외(결정성 미보장). → affected_scenarios↔TS/golden 매핑·약관 v3.0 통일은 측정 스크립트 단계에서 확정.
2. ✅ **①장애유형 산출 확장**: `defects.defect_type`(qapilot-server V19, 커밋 0d2637e) + `defect_writer._infer_defect_type`(qapilot, 커밋 0bc8489). 제품 결함에 ①(UI/API/DATA/INFRA/DOMAIN)을 ②와 별도 산출.
3. ⏳ **전용 측정 SUT + fault-triggering seed** 구성 + QApilot service 등록.
4. ⏳ **측정 스크립트**: 구성 매트릭스 × N회 실행 → verdict/defect/root_cause 수집 → 정답 대조 → 지표 산출.
5. ⏳ 1차 측정 → 목표치 재보정 → 본 문서 갱신.

---

## 부록 — 근거 코드/파일

- 유효 판정율: `QApilot-UI/src/app/pages/TestResultPage.tsx:410`, `qapilot-server .../ResultResponse.java`
- 결정 분류: `qapilot/qapilot/orchestrator/pipeline.py:_classify_failure` (4310~)
- 장애유형 매핑: `qapilot/qapilot/db/defect_writer.py:_CATEGORY_PREFIX` (29~)
- root_cause Top-N: `qapilot/qapilot/agents/root_cause_agent.py`, `shared/schemas.py:RootCauseResult`
- fault 메커니즘: `mini-bss-lite/backend/app/faults.py` (ENABLED_FAULTS env)
- ground truth: `mini-bss-lite/golden/`, `mini-bss-lite/faults/BUG-*.yaml`

## 부록 — 참고 문헌
- Pass@k / 비결정성: [Beyond pass@1 (arXiv 2603.29231)](https://arxiv.org/html/2603.29231v1), [Pass@k Metrics](https://www.emergentmind.com/topics/pass-k-metrics-2508a3b6-8dc0-488f-a854-891fb35d80b0)
- Fault localization Top-N/MRR: [LLMs in Fault Localisation (arXiv 2308.15276)](https://arxiv.org/pdf/2308.15276), [AgentFL (arXiv 2403.16362)](https://arxiv.org/pdf/2403.16362)
- 상용 벤치마크(seeded bug kill rate): [Diffblue Benchmark](https://www.diffblue.com/resources/benchmark-report-autonomous-unit-test-generation-at-enterprise-scale/), [Mabl](https://www.mabl.com/ai-test-automation)
