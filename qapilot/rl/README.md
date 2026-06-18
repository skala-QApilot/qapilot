# `qapilot/rl` — 강화학습 기반 시나리오 강화 (Phase 1)

> 이 문서는 **QApilot 은 잘 알지만 강화학습(RL)은 처음**인 사람을 위해 씁니다.
> 수식·이론은 최소화하고, "QApilot 의 무엇이 RL 의 무엇에 대응되는지"로 설명합니다.
> 설계 전체 배경은 리포 루트의 `강화학습_시나리오강화_설계서.md` 참고.

---

## 1. 한 문단 요약

QApilot 은 이미 **"시나리오를 만들어 → SUT 에서 돌려 → 결함을 잡고 → cross-check 로 판정"** 하는
루프를 돌고, 그 결과(`defects`, `tc_results`)를 DB 에 쌓습니다.
이 모듈은 그 **쌓인 결과를 "점수(보상)"로 바꿔서**, "어떤 영역을 시나리오로 치면 결함이 잘
나오는지"를 **스스로 배우게** 합니다. 그래서 다음 번 시나리오 생성 때
**"이 도메인에선 이런 영역·이런 엣지를 꼭 봐라"** 라는 힌트를 만들어 줍니다.
→ 피드백의 *"강화학습 기반으로 시나리오를 풍부하게 + 도메인 지식 축적"* 에 대한 1단계 구현.

**중요**: 이 모듈은 **LLM 을 재학습하지 않습니다.** 기존 생성 에이전트(TS/TC/TV)는
그대로 두고, 그 **앞단에 '어디를 칠지' 우선순위와 '과거에 뭐가 터졌는지' 힌트만** 붙입니다.
→ 위험이 거의 없고, 지금 있는 DB 로그만으로 바로 동작합니다.

---

## 2. 강화학습(RL) 5분 개념 — QApilot 말로 번역

RL 은 "**해보고, 점수를 받고, 점수가 높아지는 쪽으로 행동을 바꾸는**" 학습입니다.
정답지(label)가 없어도, **해본 결과로** 배웁니다. QApilot 은 "돌려봐야 좋은 시나리오인지
아는" 문제라서 RL 과 잘 맞습니다.

| RL 용어 | 쉬운 뜻 | QApilot 에서는 |
|---|---|---|
| **에이전트(agent)** | 행동을 고르는 주체 | 시나리오 생성기(TS/TC/TV) — *정책* |
| **행동(action)** | 에이전트의 선택 | "어떤 도메인 영역/엣지를 시나리오로 칠까" |
| **환경(environment)** | 행동의 결과를 돌려주는 세계 | SUT(테스트 대상 서비스) + 실행 파이프라인 |
| **보상(reward)** | 행동이 좋았는지 점수 | 진짜 결함을 잡았나? 커버리지가 늘었나? (DB 에서 계산) |
| **정책(policy)** | "상태 → 행동" 규칙 | 생성 에이전트 + 이 모듈의 우선순위/힌트 |
| **탐험 vs 활용** | 새 영역도 시도 vs 잘 되던 것 반복 | "풍부하게" = 탐험. 결함 잘 나오던 영역 = 활용 |

### 우리가 쓰는 알고리즘: **Thompson Sampling 밴딧** (가장 쉬운 RL)

"여러 슬롯머신 중 어느 것을 당길까?" 문제를 **멀티암 밴딧**이라고 합니다.
우리는 **각 '도메인 영역'을 하나의 슬롯머신(arm)** 으로 봅니다.

- 그 영역에서 시나리오가 **제품결함을 잡으면 = 성공(💰)**, 아니면 **실패**.
- 각 영역마다 성공/실패 횟수로 **"이 영역의 결함 적중률이 대략 이쯤일 것 같다"** 는
  확률 분포(Beta 분포)를 들고 있습니다.
- 다음에 칠 영역을 고를 때, 각 영역의 분포에서 **주사위를 한 번 굴려(sampling)** 가장 높은
  값이 나온 영역을 우선합니다.
  - 그래서 **적중률 높은 영역이 자주 뽑히되(활용)**, **아직 덜 시도해서 불확실한 영역도
    가끔 뽑힙니다(탐험)** → 자동으로 "풍부함"이 생깁니다.

> 비유: 맛집을 고를 때, 늘 가던 검증된 집(활용)만 가지 않고 후기가 적어 불확실한 새 집(탐험)도
> 가끔 가보는 것. Thompson Sampling 이 이 균형을 수학적으로 자동 조절합니다.

---

## 3. 두 가지 동작 — `learn` 과 `plan`

이 모듈은 두 진입점이 전부입니다.

### (A) `learn` — 런이 끝난 뒤 **배우기** (사후, 안전)
```
완료된 run_id  ──▶  defects / tc_results 읽기  ──▶  보상 계산
              ──▶  밴딧 갱신(영역별 성공/실패)
              ──▶  도메인 패턴 추출 → 뱅크(Qdrant) 적재
              ──▶  경험 저장(rl_experiences)
```
생성에 **영향을 주지 않습니다.** 이미 끝난 런을 복기할 뿐이라 위험 0.

### (B) `plan` — 다음 생성 전 **계획 세우기**
```
후보 영역들  ──▶  밴딧이 우선순위 매김(Thompson)
            ──▶  도메인 뱅크에서 과거 실패 패턴 검색
            ──▶  "우선 영역 + 엣지 힌트" 블록 생성
                  → 시나리오 생성 프롬프트에 끼워넣을 수 있음
```

---

## 4. 실제 동작 예시 (라이브 데이터로 검증됨)

실제 런 하나(`04d5f79e…`, 시나리오 35개)에 대해 `learn` 을 돌린 결과:

```jsonc
{
  "scenarios": 35,
  "product_defect_scenarios": 29,     // 35개 중 29개가 제품결함을 잡음
  "areas": {
    "성능":      { "successes": 5, "trials": 5, "posterior": 0.857 },  // 적중률 추정 최고
    "결제·청구": { "successes": 6, "trials": 7, "posterior": 0.778 },
    "회원·인증": { "successes": 3, "trials": 4, "posterior": 0.667 }   // 상대적으로 낮음
  },
  "patterns_added": 7                 // 도메인 뱅크에 실패 패턴 7건 적재
}
```

이어서 `plan` 을 돌리면, **밴딧이 우선순위를 매기고 Qdrant 에서 실제 과거 결함을 끌어와**
생성기에 줄 힌트를 만듭니다:

```
우선 검증 영역(결함 적중률 높은 순): 가입·변경, 성능, 결제·청구, ...
[결제·청구] 이 도메인에서 과거 발견된 실패 유형 — 반드시 엣지로 포함:
  · get_plan 함수에서 DB 에서 가져온 계획 데이터가 UI 에 반영되지 않아 None 이 표시되었다.
[회원·인증] ...
  · signup 화면에서 plans 데이터를 제대로 처리하지 못해 None 이 반환되었다.
```

→ 다음 시나리오 생성 때 이 힌트를 넣으면, "과거에 None 으로 터졌던 자리"를 **콕 집어**
다시 검증하게 됩니다. 이게 "도메인 지식 축적 + 풍부화"의 실체입니다.

---

## 5. 사용법

### CLI (운영/수동 실행 — 제품 UI 가 아니라 ops 잡)
```bash
# 환경변수 (없으면 인메모리로 degrade)
export DATABASE_URL="postgresql://qapilot:qapilot@localhost:5432/qapilot"
export QDRANT_URL="http://localhost:6333"

# 1) 완료된 런으로 학습
python -m qapilot.rl learn <run_id> --service <service_id>

# 2) 다음 생성용 강화 계획 출력
python -m qapilot.rl plan <service_id> --areas "결제·청구,성능,회원·인증" --query "결제 금액 검증"
```

### Celery 태스크 (런 종료 시 자동 학습 — 설계서의 'Trainer')
파이프라인 완료 지점에서 한 줄:
```python
from qapilot.rl.tasks import learn_from_run_task
learn_from_run_task.delay(run_id, service_id)   # 백그라운드 학습
```

### 파이프라인 생성 단계에 힌트 주입 (seam, opt-in)
TS 생성 노드(`_ts_generate_prd_only`)에서 생성 직전에:
```python
from qapilot.rl.service import RLService

plan = RLService().plan(
    service_id=service_id,
    candidate_areas=[r.domain_area for r in requirements],   # 후보 영역
    query=ts_name_or_requirements_text,
    domain=service_id,
)
hint_block = plan.as_prompt_hints()    # 이 문자열을 생성 프롬프트에 추가
```
> 주의: 힌트를 **실제로 쓰려면** 생성 에이전트 프롬프트가 `hint_block` 을 읽도록
> 한 줄 추가가 필요합니다. 이 부분은 생성 품질에 직접 영향을 주므로 **실런 검증과 함께**
> 켜는 것을 권장합니다(기본은 꺼짐).

---

## 6. 보상(reward)은 어떻게 계산되나 — 핵심

"좋은 시나리오"의 정의가 곧 보상입니다. 잘못 정의하면 모델이 **꼼수(reward hacking)** 를
부리므로(예: 일부러 깨지는 테스트 양산), **확정된 가치에만** 점수를 줍니다.

| 신호 | 점수 | 어디서 (이미 DB 에 있음) |
|---|---|---|
| 확정 **제품결함** 검출 | **+ (가장 큼)** | `defects.category` (제품결함 카테고리) |
| 원인추론 신뢰도 | + | `defects.root_cause_confidence` |
| cross-check **확정 판정** | + | `tc_results.status ∈ {pass, fail}` |
| 모달리티 커버리지 | + | `tc_results.kind` (ui/api/db/cross_check) |
| 새로움(novelty) | + | 도메인 뱅크와의 거리 |
| **테스트 자체 오탐** | **−** | `category=TEST_DEFECT_*` |
| **보류(검증 못함)** | **−** | `status='unverified'` |
| 중복·비용 | − | 임베딩 유사도 · 토큰 |

**꼼수 방지**: "그냥 FAIL" 이 아니라 **"확정된 제품결함"** 에만 큰 보상 → 일부러 깨지는
테스트로 점수를 못 법니다. 보류/오탐은 오히려 감점.

> 제품결함 vs 테스트오탐/환경결함 분류는 라이브 DB 의 실제 카테고리
> (`PRODUCT_DEFECT_CANDIDATE`, `UI_ERROR`, `TEST_DEFECT_UNVERIFIABLE`, `ENV_TIMEOUT` …)에
> 맞춰져 있으며 `RewardConfig` 로 바꿀 수 있습니다.

---

## 7. 파일 구조

```
qapilot/rl/
├── __init__.py        공개 API (RLService, ThompsonBandit, ...)
├── schemas.py         순수 데이터 구조 (RunOutcome, RewardConfig, DomainPattern, EnrichmentPlan ...)
├── reward.py          보상 계산 (§6) — 순수 함수, DB 의존 없음
├── bandit.py          Thompson Sampling 밴딧 — 순수, 시드로 결정적
├── domain_bank.py     도메인 지식 뱅크 — Qdrant(없으면 인메모리) + 패턴 추출
├── experience.py      경험/밴딧 영속화 (rl_experiences / rl_bandit_arms) + 인메모리 store
├── db.py              런 관측 읽기(tc_results/defects/scenarios) + 영역 해석(infer_area) + DDL
├── service.py         RLService — learn_from_run() / plan() 묶음
├── tasks.py           Celery Trainer 태스크
└── __main__.py        CLI (learn / plan)
```

**설계 원칙**: 모든 IO(DB·Qdrant·임베딩)는 **그레이스풀하게 degrade** 합니다. 인프라가 없으면
인메모리/no-op 으로 동작하므로, 순수 로직(`reward`, `bandit`, 패턴추출)은 **인프라 없이 단위
테스트**됩니다 (`tests/test_rl_*.py`, 31케이스).

---

## 8. 데이터 / 새 테이블

`learn` 첫 실행 시 자동 생성(IF NOT EXISTS):

- **`rl_experiences`** — (run, 영역, 행동, 보상, 보상 세부) 경험 로그. = 학습 데이터(리플레이 버퍼).
- **`rl_bandit_arms`** — 서비스×영역별 밴딧 사후분포(alpha/beta). 학습 상태 보존·복원.
- **Qdrant `rl_domain_patterns`** — 도메인별 실패 패턴(임베딩). 기존 `domain_knowledge` 컬렉션과 분리.

기존 테이블(`defects`/`tc_results`/`scenarios`)은 **읽기만** 합니다.

### 영역(domain_area) 해석
밴딧의 arm 은 '도메인 영역'입니다. `scenarios.payload.domain_area` 가 채워져 있으면 그대로
쓰고, **비어 있으면 시나리오 `name` 키워드로 coarse 영역을 추론**합니다
(예: "회원가입 테스트" → `회원·인증`, "성능 테스트" → `성능`). → `db.infer_area()`.

---

## 9. 지금 되는 것 / 아직 안 되는 것

**✅ 구현·검증 완료 (Phase 1)**
- 실제 런에서 보상 계산 → 밴딧 학습 → 도메인 패턴 Qdrant 적재 → 강화 계획 생성 (라이브 검증).
- LLM 미세조정 없음 → 무위험, 기존 로그만으로 동작.
- 인프라 없으면 인메모리로 degrade, 순수 로직 31 테스트.

**⏳ 다음 단계 (설계서 P2/P3 — 본 모듈 범위 밖)**
- **P2 오프라인 RFT**: 보상 높은 시나리오로 생성 LLM 미세조정 (데이터 수천 건 필요).
- **P3 GRPO 온라인**: 샘플-실행-상대보상으로 LLM 정책 직접 강화 (학습 인프라·실행 프록시 필요).
- 이들은 데이터량·GPU·평가 하네스가 갖춰진 뒤 진행합니다. 지금 "완성"이라 말하면 거짓입니다.

---

## 10. 테스트

```bash
# 인프라 불필요 (인메모리)
pytest tests/test_rl_reward.py tests/test_rl_bandit.py \
       tests/test_rl_domain_bank.py tests/test_rl_service.py tests/test_rl_area.py -q
```
