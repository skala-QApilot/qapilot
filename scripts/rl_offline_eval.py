"""RL 영역-우선순위 정책 오프라인 평가 (OPE) — Leave-One-Run-Out 교차검증.

측정 질문(정직한 범위):
    "제한된 '시나리오 예산' 하에서, 밴딧이 과거 런으로 학습한 도메인 영역 우선순위로
     테스트 순서를 정하면 — 무작위(학습 없음) 대비 제품결함을 더 일찍/더 많이 잡는가?"

이것은 **RL 정책의 영역-우선순위 품질** 을 실측 로그로 평가한다(표준 OPE).
이것이 아닌 것: '생성 LLM 이 더 좋은 시나리오를 만든다' 는 엔드투엔드 주장 — 그건 전체
파이프라인 + 결함주입 ground-truth 로 ON/OFF 를 다회 실행해야 하며 본 스크립트 범위 밖.

설계:
- 한 서비스의 런들 중 1개를 test, 나머지를 train (LORO). 서비스별 반복.
- train 런들로 밴딧 학습(영역별 성공/실패 누적). test 런에서 영역을 **사후평균 내림차순**
  으로 순위.  (Thompson 표본이 아니라 학습지식 자체를 평가 → 결정적)
- 베이스라인: 무작위 순서(몬테카를로). 상한: 오라클(test 런 실제 수율 순).
- 지표: '시나리오 예산의 f 만큼 썼을 때 잡은 제품결함 비율'(lift curve) + AULC.
- 폴드별 lift(밴딧−무작위)@50% 의 부트스트랩 95% CI.
"""

from __future__ import annotations

import os
import random
import statistics
from collections import defaultdict

from qapilot.rl import db as rl_db
from qapilot.rl.bandit import ThompsonBandit
from qapilot.rl.reward import compute_area_stats, compute_scenario_rewards

MIN_RUNS_PER_SERVICE = 3
F_GRID = [i / 20 for i in range(1, 21)]  # 0.05 ~ 1.0
RANDOM_SHUFFLES = 500
BOOTSTRAP = 4000
RNG = random.Random(20260618)


def _load_runs():
    """defects 있는 (run_id, service_id) 목록."""
    import psycopg

    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url) as c, c.cursor() as cur:
        cur.execute(
            "SELECT d.run_id::text, MIN(d.service_id::text) "
            "FROM defects d GROUP BY d.run_id HAVING count(*) > 0"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _area_stats(run_id: str, service_id: str) -> dict:
    out = rl_db.fetch_run_outcome(run_id, service_id)
    if not out:
        return {}
    return compute_area_stats(compute_scenario_rewards(out))


def _captured(order, stats, budget_scn):
    """순서대로 영역을 채택하며 budget_scn 시나리오를 쓸 때 잡은 (분수) 제품결함 수."""
    cum_scn = 0.0
    cum_def = 0.0
    for a in order:
        st = stats.get(a)
        if not st or st.trials == 0:
            continue
        if cum_scn + st.trials <= budget_scn:
            cum_scn += st.trials
            cum_def += st.successes
        else:
            rem = budget_scn - cum_scn
            if rem > 0:
                cum_def += st.successes * (rem / st.trials)
            break
    return cum_def


def _capture_curve(order, stats, N, D):
    """f-grid 에서 capture fraction(잡은결함/총결함)."""
    return [(_captured(order, stats, f * N) / D if D else 0.0) for f in F_GRID]


def _aulc(curve):
    """lift curve 아래 면적(사다리꼴), f∈[0,1] 정규화 → 0~1 (1=완벽)."""
    xs = [0.0] + F_GRID
    ys = [0.0] + curve
    area = sum((ys[i] + ys[i + 1]) / 2 * (xs[i + 1] - xs[i]) for i in range(len(xs) - 1))
    return area  # f 범위가 0~1 이므로 이미 정규화


def main():
    runs = _load_runs()
    by_service = defaultdict(list)
    for run_id, sid in runs:
        by_service[sid].append(run_id)

    folds = []  # (service, test_run, bandit_curve, random_curve, oracle_curve, N, D)
    for sid, run_ids in by_service.items():
        if len(run_ids) < MIN_RUNS_PER_SERVICE:
            continue
        stats_by_run = {r: _area_stats(r, sid) for r in run_ids}
        stats_by_run = {r: s for r, s in stats_by_run.items() if s}
        run_ids = list(stats_by_run.keys())
        if len(run_ids) < MIN_RUNS_PER_SERVICE:
            continue

        for test_run in run_ids:
            test_stats = stats_by_run[test_run]
            N = sum(s.trials for s in test_stats.values())
            D = sum(s.successes for s in test_stats.values())
            if D == 0 or N == 0:
                continue

            # --- 밴딧: train 런들로 학습 ---
            bandit = ThompsonBandit()
            for r in run_ids:
                if r == test_run:
                    continue
                bandit.update_from_stats(stats_by_run[r])
            areas = list(test_stats.keys())
            bandit_order = sorted(areas, key=lambda a: bandit.posterior_mean(a), reverse=True)
            bandit_curve = _capture_curve(bandit_order, test_stats, N, D)

            # --- 오라클: test 실제 수율 순 (상한) ---
            oracle_order = sorted(
                areas,
                key=lambda a: (test_stats[a].successes / test_stats[a].trials) if test_stats[a].trials else 0,
                reverse=True,
            )
            oracle_curve = _capture_curve(oracle_order, test_stats, N, D)

            # --- 무작위: 몬테카를로 평균 ---
            rnd_curves = []
            for _ in range(RANDOM_SHUFFLES):
                o = areas[:]
                RNG.shuffle(o)
                rnd_curves.append(_capture_curve(o, test_stats, N, D))
            random_curve = [statistics.mean(c[i] for c in rnd_curves) for i in range(len(F_GRID))]

            folds.append((sid, test_run, bandit_curve, random_curve, oracle_curve, N, D))

    if not folds:
        print("폴드 없음 (서비스별 런 부족)")
        return

    # f=0.5 인덱스
    i50 = F_GRID.index(0.5)
    i25 = F_GRID.index(0.25)
    i75 = F_GRID.index(0.75)

    def agg(curves_idx):
        return statistics.mean(curves_idx)

    print(f"# RL 영역-우선순위 OPE (Leave-One-Run-Out)")
    print(f"폴드 수(평가 런): {len(folds)}  /  서비스: {len(set(f[0] for f in folds))}")
    print(f"각 폴드: train=서비스의 나머지 런, test=1개 런\n")

    # 폴드별 capture@50
    b50 = [f[2][i50] for f in folds]
    r50 = [f[3][i50] for f in folds]
    o50 = [f[4][i50] for f in folds]
    b25 = [f[2][i25] for f in folds]; r25 = [f[3][i25] for f in folds]; o25 = [f[4][i25] for f in folds]
    b75 = [f[2][i75] for f in folds]; r75 = [f[3][i75] for f in folds]; o75 = [f[4][i75] for f in folds]
    bA = [_aulc(f[2]) for f in folds]; rA = [_aulc(f[3]) for f in folds]; oA = [_aulc(f[4]) for f in folds]

    print("## 예산별 '잡은 제품결함 비율' (평균, 전체 폴드)")
    print(f"{'예산':>6}{'무작위(베이스)':>14}{'밴딧(RL)':>12}{'오라클(상한)':>14}{'RL 리프트':>12}")
    for label, b, r, o in [("25%", b25, r25, o25), ("50%", b50, r50, o50), ("75%", b75, r75, o75)]:
        mb, mr, mo = agg(b), agg(r), agg(o)
        print(f"{label:>6}{mr:>14.3f}{mb:>12.3f}{mo:>14.3f}{(mb-mr):>+12.3f}")
    print(f"{'AULC':>6}{agg(rA):>14.3f}{agg(bA):>12.3f}{agg(oA):>14.3f}{(agg(bA)-agg(rA)):>+12.3f}")

    # 부트스트랩 CI of lift@50 (밴딧-무작위, 폴드 페어)
    lifts = [b - r for b, r in zip(b50, r50)]
    boot = []
    for _ in range(BOOTSTRAP):
        sample = [lifts[RNG.randrange(len(lifts))] for _ in lifts]
        boot.append(statistics.mean(sample))
    boot.sort()
    lo = boot[int(0.025 * BOOTSTRAP)]
    hi = boot[int(0.975 * BOOTSTRAP)]
    mean_lift = statistics.mean(lifts)
    # 정규화 헤드룸: RL 이 (오라클-무작위) 갭의 몇 %를 메웠나
    gap = agg(o50) - agg(r50)
    closed = (agg(b50) - agg(r50)) / gap if gap > 1e-9 else float("nan")

    print(f"\n## 핵심 결과 (예산 50%)")
    print(f"- 무작위 베이스라인: {agg(r50):.1%} 의 제품결함 포착")
    print(f"- RL(밴딧):        {agg(b50):.1%}  (리프트 {mean_lift:+.1%}p)")
    print(f"- 오라클 상한:      {agg(o50):.1%}")
    print(f"- 리프트 95% CI (부트스트랩, n={len(folds)} 폴드): [{lo:+.1%}p, {hi:+.1%}p]")
    print(f"- 유의성: {'유의 (CI 가 0 제외)' if lo > 0 else '불충분 (CI 가 0 포함)'}")
    print(f"- 오라클 대비 메운 갭: {closed:.0%}  (RL 이 '완벽 사전지식' 헤드룸의 {closed:.0%}를 회수)")

    # 서비스별
    print(f"\n## 서비스별 (예산 50% capture)")
    print(f"{'service':>10}{'folds':>7}{'D(결함)':>9}{'무작위':>9}{'RL':>8}{'오라클':>9}{'리프트':>9}")
    bysvc = defaultdict(list)
    for f in folds:
        bysvc[f[0]].append(f)
    svc_lifts = []
    for sid, fs in bysvc.items():
        mr = statistics.mean(f[3][i50] for f in fs)
        mb = statistics.mean(f[2][i50] for f in fs)
        mo = statistics.mean(f[4][i50] for f in fs)
        mD = statistics.mean(f[6] for f in fs)
        svc_lifts.append(mb - mr)
        print(f"{sid[:8]:>10}{len(fs):>7}{mD:>9.1f}{mr:>9.2f}{mb:>8.2f}{mo:>9.2f}{(mb-mr):>+9.2f}")
    print(f"\n- 서비스 **매크로 평균** 리프트@50%: {statistics.mean(svc_lifts):+.1%}p "
          f"(서비스별 동일가중 — 폴드수 편향 제거)")

    # 비-degenerate 부분집합 (결함 >= 3건: 우선순위가 '의미 있는' 런만)
    nd = [f for f in folds if f[6] >= 3]
    print(f"\n## 비-degenerate 부분집합 (D≥3, 결함 1~2건 saturated 런 제외)")
    print(f"폴드 {len(nd)}개")
    if nd:
        nr = statistics.mean(f[3][i50] for f in nd)
        nb = statistics.mean(f[2][i50] for f in nd)
        no = statistics.mean(f[4][i50] for f in nd)
        nlifts = [f[2][i50] - f[3][i50] for f in nd]
        nboot = []
        for _ in range(BOOTSTRAP):
            s = [nlifts[RNG.randrange(len(nlifts))] for _ in nlifts]
            nboot.append(statistics.mean(s))
        nboot.sort()
        nlo, nhi = nboot[int(0.025 * BOOTSTRAP)], nboot[int(0.975 * BOOTSTRAP)]
        print(f"- 무작위 {nr:.1%} / RL {nb:.1%} / 오라클 {no:.1%}  → 리프트 {nb-nr:+.1%}p")
        print(f"- 리프트 95% CI: [{nlo:+.1%}p, {nhi:+.1%}p]  "
              f"({'유의' if nlo > 0 else '불충분'})")


if __name__ == "__main__":
    main()
