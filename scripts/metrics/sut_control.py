"""측정 SUT 제어 — docker-compose 구성(결함 토글) 전환 + 헬스 대기.

측정 설계서 §2 운영 절차:
- faults.py 는 import 시 env 를 읽음 → 구성 변경 시 컨테이너 재기동 필수.
- seed 는 멱등(기존 계정 skip) → birth_date 신규 seed 적용하려면 `down -v`(볼륨 삭제) 필수.
  ∴ 구성마다 down -v 로 pristine 시작(결함 누적·stale seed 방지).
"""
from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

import config


def _compose(args: list[str], env_faults: str, cwd: Path) -> subprocess.CompletedProcess:
    """ENABLED_FAULTS 를 주입해 docker compose 실행."""
    import os
    env = dict(os.environ)
    env["ENABLED_FAULTS"] = env_faults
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def wait_healthy(urls: list[str], retries: int = 60, interval: float = 3.0) -> bool:
    """모든 URL 이 응답할 때까지 대기."""
    for _ in range(retries):
        if all(_http_ok(u) for u in urls):
            return True
        time.sleep(interval)
    return False


def bring_up(config_name: str, fresh_volume: bool = True) -> bool:
    """SUT 를 주어진 결함 구성으로 pristine 기동.

    fresh_volume=True 면 down -v 로 볼륨 삭제 후 up (seed 재적용 보장).
    반환: 모든 서비스 healthy 면 True.
    """
    sut_root = config.SUT_ROOT
    faults_env = config.enabled_faults_env(config_name)
    if fresh_volume:
        _compose(["down", "-v"], faults_env, sut_root)
    else:
        _compose(["down"], faults_env, sut_root)
    up = _compose(["up", "-d", "--build"], faults_env, sut_root)
    if up.returncode != 0:
        print(f"[sut] compose up 실패 ({config_name}):\n{up.stderr[-800:]}")
        return False
    ok = wait_healthy(config.HEALTH_URLS)
    print(f"[sut] {config_name} 기동 {'OK' if ok else '실패(헬스 타임아웃)'} "
          f"(ENABLED_FAULTS='{faults_env}')")
    return ok


def tear_down() -> None:
    _compose(["down", "-v"], "", config.SUT_ROOT)


def verify_config(config_name: str) -> dict:
    """결함 토글이 실제로 반영됐는지 1건 스모크 검증 (선택).

    clean: 성인→YOUTH_SAFE 주문 → 400 기대. RULE-004 ON: → 201 기대.
    측정 전 SUT 상태 sanity check 로 사용.
    """
    import json
    b = config.BACKEND_URL
    # 로그인
    def _post(path, payload, token=None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(b + path, data=data,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
        except Exception:
            return None, {}
    _, body = _post("/api/auth/login", config.TEST_ACCOUNT)
    token = body.get("token")
    if not token:
        return {"ok": False, "reason": "login 실패"}
    # 성인 → YOUTH_SAFE(plan 6) 주문
    status, _ = _post("/api/orders", {"items": [{"plan_id": 6, "qty": 1}]}, token)
    expect = 201 if config_name == "BUG-RULE-004" else 400
    return {"ok": status == expect, "status": status, "expected": expect,
            "config": config_name}
