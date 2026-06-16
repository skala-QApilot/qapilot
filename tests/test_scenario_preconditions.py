"""scenario_preconditions.apply_preconditions 규칙 검증."""

from qapilot.shared.scenario_preconditions import apply_preconditions


def _scn(*givens):
    return [{"ts_id": "TS-001", "test_cases": [
        {"tc_id": f"TC-{i}", "given": g} for i, g in enumerate(givens, 1)
    ]}]


def test_login_given_gets_customer():
    s = _scn("유효한 고객으로 로그인한 상태에서")
    n = apply_preconditions(s)
    tc = s[0]["test_cases"][0]
    assert n == 1
    assert tc["db_check_sql"] == "SELECT 1 FROM customers WHERE id = 1"
    assert tc["db_seed_sql"].startswith("INSERT INTO customers")


def test_subscription_given_gets_order():
    s = _scn("사용자가 현재 이용 중인 요금제를 보유한 상태이며")
    apply_preconditions(s)
    tc = s[0]["test_cases"][0]
    assert tc["db_check_sql"] == "SELECT 1 FROM orders WHERE customer_id = 1"
    assert tc["db_seed_sql"].startswith("INSERT INTO orders")


def test_other_customer_given_gets_customer2():
    s = _scn("다른 고객의 인증 정보가 있는 상태에서")
    apply_preconditions(s)
    assert s[0]["test_cases"][0]["db_check_sql"] == "SELECT 1 FROM customers WHERE id = 2"


def test_signup_and_negative_skipped():
    s = _scn(
        "사용자가 회원가입 화면에서 이메일을 입력한다",
        "비로그인 상태에서",
        "사용자가 만료된 JWT를 가진 상태이다",
    )
    n = apply_preconditions(s)
    assert n == 0
    for tc in s[0]["test_cases"]:
        assert "db_check_sql" not in tc and "db_seed_sql" not in tc


def test_existing_value_preserved():
    s = [{"test_cases": [{"tc_id": "TC-1", "given": "로그인한 상태에서",
                          "db_check_sql": "SELECT 1 FROM foo", "db_seed_sql": "INSERT INTO foo"}]}]
    n = apply_preconditions(s)
    assert n == 0  # 기존 값 보존, 덮어쓰지 않음
    assert s[0]["test_cases"][0]["db_check_sql"] == "SELECT 1 FROM foo"
