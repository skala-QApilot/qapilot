# QApilot 성능 측정 스크립트

측정 설계서(`docs/metrics/measurement-design.md`)의 산식을 구현한 측정 도구.
구성 매트릭스 × N회 → ground truth 대조 → 지표 산출.

## 구성
| 파일 | 역할 |
|---|---|
| `config.py` | SUT 상태 매트릭스(clean + 신뢰 fault 5종) · 반복 N · 경로 · SUT URL |
| `ground_truth.py` | golden(114 TC, clean 오라클) + faults(7, 결함 정답) 로더. 결함↔endpoint 매핑 |
| `scoring.py` | **결정적 채점 핵심** — §3.1~3.5 산식 (유효판정율·테스트코드정확도·검출·분류①∧②·원인 Top-N/MRR) + §4 pass^k |
| `sut_control.py` | docker-compose 결함 구성 전환(down -v + ENABLED_FAULTS) + 헬스 대기 + sanity |
| `run_measurement.py` | 오케스트레이터 — 구성×N → 수집 → 채점 → `metrics_report.json` |
| `tests/test_scoring.py` | scoring 산식 단위검증 (11건, SUT 불필요) |

## 측정 매트릭스 (설계서 §1.1·§2)
`clean` + `BUG-RULE-001/002/003/004` + `BUG-API-001` = **6 구성**.
DATA-001·INFRA-001 은 비결정성으로 제외(결정성 확보 후 편입). 정책 v3 고정.

## 사전 조건
1. 측정 SUT 가 로컬 docker-compose 로 기동 가능 (`mini-bss-lite/`). `ENABLED_FAULTS` 플러밍·
   fault-triggering seed 적용 완료(브랜치 `feat/juhwan/metrics-measurement`).
2. QApilot 가 SUT 를 테스트할 준비 (등록된 service 또는 `.qapilot/scenarios` + generate 선행).
3. `pip install pyyaml pytest` (pyyaml 은 SUT 의존성에 포함).

## 사용
```bash
cd qapilot/scripts/metrics

# 1) 산식 단위검증 (SUT 불필요)
python -m pytest tests/ -q

# 2) 채점만 — 이미 수집된 run 아티팩트로 (.qapilot/metrics/runs/<config>__run<N>.json)
python run_measurement.py --score-only

# 3) 전체 측정 — SUT 구성 전환 + QApilot 실행 + 채점
#    (구성마다 docker compose down -v 로 pristine 기동 → 수 분 소요)
python run_measurement.py --configs clean BUG-RULE-004 --n 3
```

## 출력
`.qapilot/metrics/metrics_report.json` — 구성별:
- **clean**: 테스트 코드 정확도(mean±std), 유효 판정율, 오탐(FP) 수.
- **fault**: 검출 pass^k/rate, 분류 ①장애유형·②결정분류·①∧², 원인 Top-1/3/5 + MRR.

## 수집 어댑터 (run_measurement.invoke_qapilot_live)
기본은 `run_pipeline(staging_url, test_account)` 직접 호출 → state 의
`cross_check_results` + `root_cause_results` 추출. 시나리오 소스(등록 service vs 로컬
generate)는 환경 의존이므로, 라이브 수집 전 QApilot 가 SUT 시나리오를 갖춘 상태여야 한다.
아티팩트(JSON)만 있으면 `--score-only` 로 SUT/QApilot 없이 반복 채점 가능.

## 운영 주의 (설계서 §2)
- `faults.py` 는 import 시 env 를 읽음 → 구성 변경마다 컨테이너 재기동.
- seed 멱등(기존 계정 skip) → `down -v` 로 볼륨 비워야 birth_date seed 재적용. 구성마다 pristine.
- 단일 SUT 한계: N회 반복은 표본 확대가 아님(비결정성 안정화용). "본 SUT 기준" 명시.
