from app.signals import scanner

RAW = {
    "return_code": 0,
    "trde_prica_upper": [  # 배열 키 이름은 TR 마다 달라 generic 탐색을 검증
        {"stk_cd": "A123456", "stk_nm": "급등주", "cur_prc": "+015000",
         "flu_rt": "+8.5", "trde_prica": "25000"},
        {"stk_cd": "A234567", "stk_nm": "대형주", "cur_prc": "-070000",
         "flu_rt": "-1.2", "trde_prica": "90000"},
        {"stk_cd": "A345678", "stk_nm": "잡주", "cur_prc": "+000800",
         "flu_rt": "+15.0", "trde_prica": "20000"},
        {"stk_cd": "A456789", "stk_nm": "저유동성", "cur_prc": "+005000",
         "flu_rt": "+9.0", "trde_prica": "500"},
    ],
}
CFG = {"min_change_pct": 3.0, "min_trade_value": 10_000, "min_price": 1_000, "top_n": 10}


def test_parse_rank_generic_extraction():
    items = scanner.parse_rank(RAW)
    assert len(items) == 4
    assert items[0]["code"] == "123456"          # A 접두 제거
    assert items[0]["price"] == 15_000           # 부호 제거
    assert items[0]["change_pct"] == 8.5
    assert items[1]["change_pct"] == -1.2        # 하락 부호 유지


def test_filter_candidates_rules():
    picked = scanner.filter_candidates(scanner.parse_rank(RAW), CFG)
    codes = [p["code"] for p in picked]
    assert "123456" in codes      # 통과
    assert "234567" not in codes  # 등락률 미달
    assert "345678" not in codes  # 저가주 제외
    assert "456789" not in codes  # 거래대금 미달


def test_filter_excludes_existing_watchlist(monkeypatch):
    monkeypatch.setattr(scanner.settings, "WATCHLIST", {"123456": "급등주"})
    picked = scanner.filter_candidates(scanner.parse_rank(RAW), CFG)
    assert all(p["code"] != "123456" for p in picked)


def test_filter_sorts_by_change_and_caps_top_n():
    many = {
        "list": [
            {"stk_cd": f"A10{i:04d}", "stk_nm": f"s{i}", "cur_prc": "10000",
             "flu_rt": str(3 + i), "trde_prica": "99999"}
            for i in range(15)
        ]
    }
    picked = scanner.filter_candidates(scanner.parse_rank(many), dict(CFG, top_n=5))
    assert len(picked) == 5
    assert picked[0]["change_pct"] > picked[-1]["change_pct"]


def test_parse_rank_empty():
    assert scanner.parse_rank({"return_code": 0}) == []
