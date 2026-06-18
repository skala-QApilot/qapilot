"""영역 해석(infer_area) 단위 테스트 — domain_area 부재 시 name 폴백."""

from __future__ import annotations

from qapilot.rl.db import infer_area


class TestInferArea:
    def test_explicit_domain_area_wins(self):
        assert infer_area({"domain_area": "결제", "name": "로그인 테스트"}) == "결제"

    def test_coalesce_alternate_keys(self):
        assert infer_area({"area": "청구"}) == "청구"
        assert infer_area({"category": "회원"}) == "회원"

    def test_name_keyword_fallback(self):
        assert infer_area({"name": "사용자 회원가입 테스트"}) == "회원·인증"
        assert infer_area({"name": "사용자 로그인 테스트"}) == "회원·인증"
        assert infer_area({"name": "화면 진입 성능 테스트"}) == "성능"
        assert infer_area({"name": "요금 결제 테스트"}) == "결제·청구"
        assert infer_area({"name": "사용자 정보 조회 테스트"}) == "조회·검색"

    def test_unknown(self):
        assert infer_area({}) == "unknown"
        assert infer_area({"name": "그 외 무언가"}) == "unknown"
