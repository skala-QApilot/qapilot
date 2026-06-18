"""RL 모듈 CLI.

    python -m qapilot.rl learn <run_id> [--service <id>] [--domain <name>]
    python -m qapilot.rl plan  <service_id> --areas 결제,청구,회원 [--query ...] [--seed 0]

`learn` 은 완료된 런으로 사후 학습(밴딧/도메인뱅크/경험 갱신).
`plan` 은 다음 생성에 쓸 영역 우선순위 + 도메인 패턴 힌트를 출력.
"""

from __future__ import annotations

import argparse
import json
import sys

from qapilot.rl.service import RLService


def _cmd_learn(args: argparse.Namespace) -> int:
    svc = RLService()
    summary = svc.learn_from_run(args.run_id, service_id=args.service, domain=args.domain)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") == "ok" else 1


def _cmd_plan(args: argparse.Namespace) -> int:
    svc = RLService()
    areas = [a.strip() for a in (args.areas or "").split(",") if a.strip()]
    plan = svc.plan(
        service_id=args.service_id,
        candidate_areas=areas,
        query=args.query,
        domain=args.domain,
        n=args.n,
        seed=args.seed,
    )
    print(json.dumps(
        {
            "service_id": plan.service_id,
            "prioritized_areas": plan.prioritized_areas,
            "patterns_by_area": plan.patterns_by_area,
            "notes": plan.notes,
            "prompt_hints": plan.as_prompt_hints(),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qapilot.rl", description="RL 시나리오 강화 (P1)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_learn = sub.add_parser("learn", help="완료된 런으로 사후 학습")
    p_learn.add_argument("run_id")
    p_learn.add_argument("--service", default=None)
    p_learn.add_argument("--domain", default=None)
    p_learn.set_defaults(func=_cmd_learn)

    p_plan = sub.add_parser("plan", help="다음 생성용 강화 계획 출력")
    p_plan.add_argument("service_id")
    p_plan.add_argument("--areas", required=True, help="콤마구분 후보 영역")
    p_plan.add_argument("--query", default=None)
    p_plan.add_argument("--domain", default=None)
    p_plan.add_argument("--n", type=int, default=None)
    p_plan.add_argument("--seed", type=int, default=None)
    p_plan.set_defaults(func=_cmd_plan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
