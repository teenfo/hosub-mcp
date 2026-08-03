"""매매일지 — 신호 기록·집계·요인 추출·누적. 수치는 전부 결정론이어야 한다."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from app import journal, settings
from app.trade import ledger


@pytest.fixture
def db(tmp_path, monkeypatch):
    """일지·장부가 같은 tmp DB 를 보게 맞춘다(실 데이터 비의존)."""
    monkeypatch.setattr(journal, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(ledger, "DB_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(settings, "WATCHLIST", {"005930": "삼성전자"})
    monkeypatch.setattr(settings, "COSTS", {"commission_pct": 0.015,
                                            "sell_tax_pct": 0.15, "slippage_bp": 5})
    return tmp_path


def _rec(symbol="005930", rule="orb", qty=2, actionable=True, note=None,
         priority=115.0, ts="2026-07-27T09:20:00+09:00"):
    return {"symbol": symbol, "name": "종목" + symbol, "rule": rule, "side": "long",
            "entry": 10_000, "stop": 9_800, "target": 10_400, "qty": qty,
            "priority": priority, "actionable": actionable, "note": note,
            "ts": ts, "order_id": "oid" if actionable else None}


# --- 신호 기록 ---

def test_signal_upsert_keeps_one_row_per_day(db):
    journal.record_signal("2026-07-27", _rec(actionable=False, note="잔고 부족 — …"))
    journal.record_signal("2026-07-27", _rec(actionable=True, qty=3))   # 차단→해제
    rows = journal.signals_on("2026-07-27")
    assert len(rows) == 1                       # 하루·종목·규칙당 한 행
    assert rows[0]["actionable"] == 1 and rows[0]["qty"] == 3   # 최종 상태로 갱신


def test_signals_isolated_by_day(db):
    journal.record_signal("2026-07-27", _rec())
    journal.record_signal("2026-07-26", _rec())
    assert len(journal.signals_on("2026-07-27")) == 1


def test_prune_signals(db):
    journal.record_signal("2020-01-01", _rec())
    journal.record_signal("2026-07-27", _rec(symbol="000660"))
    assert journal.prune_signals(keep_days=30) == 1
    assert journal.signals_on("2020-01-01") == []


# --- 사유 라벨 ---

def test_note_labels():
    assert journal.note_label("잔고 부족 — 1주 ≈ 15,000원 > 가용 5,000원") == "잔고 부족"
    assert journal.note_label("비중 상한 — …") == "비중 상한"
    assert journal.note_label("진입 보류 — 최대 동시 포지션(3) 도달") == "포지션 한도"
    assert journal.note_label("롱 전용 모드 — 숏 미발주") == "숏 미발주"
    assert journal.note_label("알 수 없는 사유") == "기타"
    assert journal.note_label("") == "—"


# --- 집계 ---

def _order(oid, symbol="005930", rule="orb", entry=10_000, stop=9_800, target=10_400):
    return {"id": oid, "symbol": symbol, "side": "long", "entry": entry,
            "stop": stop, "target": target, "rule": rule, "qty": 10}


def test_build_aggregates_closed_trades(db):
    ledger.open_position(_order("a1"), fill=10_000)
    ledger.monitor(lambda s: 10_500)                       # 목표 청산(승)
    ledger.open_position(_order("a2", symbol="000660"), fill=10_000)
    ledger.monitor(lambda s: 9_700)                        # 손절 청산(패)
    day = ledger.positions(status="closed")[0]["closed"][:10]

    entry = journal.build(day)
    assert entry["trades"]["trades"] == 2
    assert entry["trades"]["wins"] == 1 and entry["trades"]["losses"] == 1
    assert entry["trades"]["win_rate"] == 50.0
    assert set(entry["by_exit_reason"]) == {"target", "stop"}
    assert entry["by_rule"]["orb"]["trades"] == 2


# --- 결과 구분 — 사유가 손절이어도 이익이면 익절이다 ---
#
# 사용자 지침(2026-07-30): "실제 손익이 + 이면, 청산 사유가 손절라인 근접이어도
# 익절로 표기한다." 라인 추종이 손절선을 진입가 위로 올리면 그 선에 닿아 나온 것도
# 이익이다. 사유 이름만 세면 "손절이 늘었다" 로 잘못 읽는다.

def test_손절선에서_이익으로_끝나면_익절로_센다(db):
    ledger.open_position(_order("w1", entry=10_000, stop=9_800), fill=10_000)
    pid = ledger.positions(status="open")[0]["id"]
    # 라인 추종이 손절선을 진입가 위로 올린 뒤 그 선에서 청산됐다.
    # 데스크가 실제로 하는 것과 같은 경로다 — 사유는 stop, 결과는 이익.
    ledger.set_lines(pid, 10_300, None)
    ledger.close_position(pid, 10_250, "stop")
    p = ledger.positions(status="closed")[0]
    assert p["exit_reason"] == "stop", "기계적 사유는 덮어쓰지 않는다"
    assert p["pnl_krw"] > 0
    assert p["outcome"] == "익절"

    day = p["closed"][:10]
    e = journal.build(day)
    assert e["by_outcome"]["익절"]["trades"] == 1
    assert e["by_exit_reason"]["stop"]["trades"] == 1, "사유 집계는 그대로 남는다"
    assert any("손절선에서 이익" in f for f in e["facts"])
    assert "익절(stop)" in journal.summary_prompt(e)


def test_결과_구분은_손익_부호를_따른다(db):
    ledger.open_position(_order("l1"), fill=10_000)
    ledger.monitor(lambda s: 9_700)
    assert ledger.positions(status="closed")[0]["outcome"] == "손절"


def test_청산_전에는_결과가_없다(db):
    ledger.open_position(_order("o1"), fill=10_000)
    assert ledger.positions(status="open")[0]["outcome"] is None, \
        "없는 결과를 만들지 않는다"


def test_build_counts_blocked_signals(db):
    journal.record_signal("2026-07-27", _rec(actionable=True))
    journal.record_signal("2026-07-27", _rec(symbol="000660", actionable=False,
                                             note="비중 상한 — …"))
    journal.record_signal("2026-07-27", _rec(symbol="000100", actionable=False,
                                             note="잔고 부족 — …"))
    sig = journal.build("2026-07-27")["signals"]
    assert sig == sig | {"total": 3, "ordered": 1, "blocked": 2}
    assert sig["by_note"] == {"비중 상한": 1, "잔고 부족": 1}


def test_build_is_deterministic(db):
    journal.record_signal("2026-07-27", _rec())
    a, b = journal.build("2026-07-27"), journal.build("2026-07-27")
    a.pop("generated"), b.pop("generated")     # 생성 시각만 다르다
    assert a == b


# --- 요인 추출 ---

def _entry(**over):
    base = {
        "date": "2026-07-27",
        "trades": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                   "pnl_krw": 0.0, "expectancy_pct": 0.0, "avg_slippage_pct": 0.0},
        "by_rule": {}, "by_exit_reason": {}, "by_hour": {},
        "positions": [], "carried": [],
        "signals": {"total": 0, "ordered": 0, "blocked": 0, "by_note": {}},
        "guard": {},
    }
    base.update(over)
    return base


def test_facts_no_trades():
    f = journal.facts(_entry())
    assert any("청산된 거래 없음" in x for x in f)
    assert any("기록된 신호 없음" in x for x in f)


def test_facts_flags_eod_heavy_day():
    f = journal.facts(_entry(
        trades={"trades": 4, "wins": 1, "losses": 3, "win_rate": 25.0,
                "pnl_krw": -5000, "expectancy_pct": -0.4, "avg_slippage_pct": 0.0},
        by_exit_reason={"eod": {"trades": 3, "expectancy_pct": -0.2},
                        "stop": {"trades": 1, "expectancy_pct": -1.0}}))
    assert any("마감 정리(eod)" in x for x in f)


def test_facts_flags_capital_blocks():
    f = journal.facts(_entry(
        signals={"total": 20, "ordered": 4, "blocked": 16,
                 "by_note": {"잔고 부족": 16}}))
    assert any("자금·한도로 밀린 신호 16건" in x for x in f)


def test_facts_flags_slippage():
    f = journal.facts(_entry(
        trades={"trades": 2, "wins": 1, "losses": 1, "win_rate": 50.0,
                "pnl_krw": 0, "expectancy_pct": 0.0, "avg_slippage_pct": 0.55},
        by_exit_reason={"target": {"trades": 2, "expectancy_pct": 0.0}}))
    assert any("슬리피지" in x for x in f)


def test_facts_flags_losing_hours():
    f = journal.facts(_entry(
        trades={"trades": 3, "wins": 0, "losses": 3, "win_rate": 0.0,
                "pnl_krw": -3000, "expectancy_pct": -1.0, "avg_slippage_pct": 0.0},
        by_exit_reason={"stop": {"trades": 3, "expectancy_pct": -1.0}},
        by_hour={"11": {"trades": 2, "expectancy_pct": -1.2},
                 "9": {"trades": 1, "expectancy_pct": 0.5}}))
    assert any("시간대 손실 구간" in x and "11시" in x for x in f)


def test_facts_flags_carried_positions():
    assert any("미청산 포지션" in x
               for x in journal.facts(_entry(carried=[{"id": "x"}])))


# --- 저장·누적 ---

def test_save_load_roundtrip(db):
    entry = journal.build("2026-07-27")
    journal.save(entry)
    assert journal.load("2026-07-27")["date"] == "2026-07-27"
    assert journal.load("2026-01-01") is None      # 없는 날짜는 None


def test_history_and_cumulative(db):
    for day, trades, pnl, wr in (("2026-07-24", 2, 1000.0, 100.0),
                                 ("2026-07-27", 3, -400.0, 33.3)):
        journal.save({"date": day, "generated": "x",
                      "trades": {"trades": trades, "win_rate": wr, "pnl_krw": pnl,
                                 "expectancy_pct": 0.5},
                      "signals": {"total": 5, "blocked": 1}, "summary": {}})
    rows = journal.history(30)
    assert [r["date"] for r in rows] == ["2026-07-27", "2026-07-24"]   # 최신순
    c = journal.cumulative(rows)
    assert c["trades"] == 5 and c["trading_days"] == 2
    assert c["pnl_krw"] == 600.0
    assert c["best_day"] == "2026-07-24" and c["worst_day"] == "2026-07-27"


def test_cumulative_ignores_no_trade_days(db):
    rows = [{"date": "2026-07-27", "trades": 0, "win_rate": 0.0, "pnl_krw": 0.0,
             "expectancy_pct": 0.0, "signals": 0, "blocked": 0},
            {"date": "2026-07-24", "trades": 2, "win_rate": 50.0, "pnl_krw": 500.0,
             "expectancy_pct": 1.0, "signals": 3, "blocked": 1}]
    c = journal.cumulative(rows)
    assert c["days"] == 2 and c["trading_days"] == 1
    assert c["win_rate"] == 50.0                  # 거래 없는 날이 평균을 희석하지 않는다


def test_cumulative_empty():
    c = journal.cumulative([])
    assert c["trades"] == 0 and c["best_day"] is None


def test_history_skips_corrupt_files(db):
    journal.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    (journal.JOURNAL_DIR / "2026-07-27.json").write_text("{깨진 JSON")
    assert journal.history(30) == []              # 예외 없이 건너뛴다


# --- LLM 요약 (게이트웨이 장애 내성) ---

@pytest.mark.asyncio
async def test_summarize_failure_does_not_break_journal(db, monkeypatch):
    """게이트웨이가 죽어도 일지는 만들어진다 — 요약만 비고 사유가 남는다."""
    from app import llmgw

    class Boom:
        def __init__(self, *a, **kw):
            raise llmgw.GatewayError("연결 실패")

    monkeypatch.setattr(llmgw, "AsyncLLMGateway", Boom)
    monkeypatch.setattr(journal, "summary_ready", lambda day, now=None: (True, ""))
    entry = await journal.generate("2026-07-27")
    assert entry["summary"]["ok"] is False and "연결 실패" in entry["summary"]["error"]
    assert entry["facts"]                          # 결정론 부분은 그대로
    assert journal.load("2026-07-27") is not None  # 저장도 정상


@pytest.mark.asyncio
async def test_summarize_success(db, monkeypatch):
    from app import llmgw

    class Fake:
        def __init__(self, *a, **kw):
            pass

        async def run(self, role, prompt, **kw):
            assert "관찰 사실" in prompt          # 원자료가 아니라 사실만 넘긴다
            return "  오늘의 결과: 거래 없음  "

    monkeypatch.setattr(llmgw, "AsyncLLMGateway", Fake)
    monkeypatch.setattr(journal, "summary_ready", lambda day, now=None: (True, ""))
    entry = await journal.generate("2026-07-27")
    assert entry["summary"]["ok"] is True
    assert entry["summary"]["text"] == "오늘의 결과: 거래 없음"


def test_summary_prompt_carries_only_facts(db):
    entry = journal.build("2026-07-27")
    entry["facts"] = ["사실1", "사실2"]
    p = journal.summary_prompt(entry)
    assert "- 사실1" in p and "- 사실2" in p
    assert "매수" not in journal.SUMMARY_SYSTEM.split("규칙:")[0]   # 매매 지시 아님
    assert "추천" in journal.SUMMARY_SYSTEM                        # 추천 금지 명시


# --- 요약은 장 마감 후에만 ---
# 장중 요약은 '오전까지의 손익'을 하루의 결론처럼 쓰게 되어 오해를 부른다.

def _now(h, m, day=27):
    return datetime(2026, 7, day, h, m, tzinfo=ZoneInfo("Asia/Seoul"))


def test_summary_ready_only_after_close(db, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "journal", {"run_after": "15:45"})
    ok, why = journal.summary_ready("2026-07-27", _now(11, 30))
    assert ok is False and "15:45" in why          # 장중
    assert journal.summary_ready("2026-07-27", _now(15, 44))[0] is False
    assert journal.summary_ready("2026-07-27", _now(15, 45))[0] is True
    assert journal.summary_ready("2026-07-27", _now(16, 30))[0] is True


def test_summary_ready_past_and_future(db, monkeypatch):
    monkeypatch.setitem(settings.CONFIG, "journal", {"run_after": "15:45"})
    # 지난 날짜는 이미 끝났으므로 장중에 조회해도 작성 가능
    assert journal.summary_ready("2026-07-24", _now(11, 30))[0] is True
    ok, why = journal.summary_ready("2026-07-28", _now(11, 30))
    assert ok is False and "아직 오지 않은" in why


@pytest.mark.asyncio
async def test_generate_skips_summary_intraday(db, monkeypatch):
    """장중 '다시 작성'은 집계만 갱신하고 LLM 을 부르지 않는다."""
    from app import llmgw

    called = []

    class Fake:
        def __init__(self, *a, **kw):
            called.append(1)

        async def run(self, *a, **kw):
            return "x"

    monkeypatch.setattr(llmgw, "AsyncLLMGateway", Fake)
    monkeypatch.setattr(journal, "summary_ready",
                        lambda day, now=None: (False, "장 마감 후(15:45 이후) 자동 작성됩니다"))
    entry = await journal.generate("2026-07-27")
    assert called == []                             # 게이트웨이 호출 없음
    assert entry["final"] is False
    assert entry["summary"]["pending"] is True
    assert "15:45" in entry["summary"]["reason"]
    assert entry["facts"]                           # 집계는 그대로


@pytest.mark.asyncio
async def test_generate_writes_summary_after_close(db, monkeypatch):
    from app import llmgw

    class Fake:
        def __init__(self, *a, **kw):
            pass

        async def run(self, *a, **kw):
            return "오늘의 결과: 정리"

    monkeypatch.setattr(llmgw, "AsyncLLMGateway", Fake)
    monkeypatch.setattr(journal, "summary_ready", lambda day, now=None: (True, ""))
    entry = await journal.generate("2026-07-27")
    assert entry["final"] is True
    assert entry["summary"]["text"] == "오늘의 결과: 정리"


def test_history_reports_final_flag(db):
    journal.save({"date": "2026-07-27", "generated": "x", "final": False,
                  "trades": {"trades": 0}, "signals": {}, "summary": {}})
    journal.save({"date": "2026-07-24", "generated": "x", "final": True,
                  "trades": {"trades": 1, "win_rate": 100.0, "pnl_krw": 10.0,
                             "expectancy_pct": 1.0},
                  "signals": {"total": 1, "blocked": 0},
                  "summary": {"text": "요약"}})
    rows = {r["date"]: r for r in journal.history(30)}
    assert rows["2026-07-27"]["final"] is False and rows["2026-07-27"]["has_summary"] is False
    assert rows["2026-07-24"]["final"] is True and rows["2026-07-24"]["has_summary"] is True


def test_journal_records_guard_override(db, monkeypatch):
    """가드를 열고 낸 하루라는 사실이 일지에 남아야 한다 — 성과 해석이 달라진다."""
    from app.trade import override

    monkeypatch.setattr(override, "FILE", db / "guard_override.json")
    monkeypatch.setitem(settings.RISK, "guard_override_max_pct", 2.0)
    monkeypatch.setitem(settings.RISK, "guard_override_max_count", 2)
    now = datetime(2026, 7, 27, 11, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    override.grant(mode="reset", extra_pct=1.86, pct_at=-1.86, now=now)

    entry = journal.build("2026-07-27")
    assert entry["guard"]["override"]["extra_loss_pct"] == 1.86
    assert any("임시 해제" in f and "같은 기준으로 비교할 수 없다" in f
               for f in entry["facts"])
    assert journal.build("2026-07-24")["guard"] == {}   # 다른 날엔 안 붙는다


def test_facts_flags_timeout_exits():
    f = journal.facts(_entry(
        trades={"trades": 3, "wins": 0, "losses": 3, "win_rate": 0.0,
                "pnl_krw": -500, "expectancy_pct": -0.3, "avg_slippage_pct": 0.0},
        by_exit_reason={"timeout": {"trades": 2, "expectancy_pct": -0.2},
                        "stop": {"trades": 1, "expectancy_pct": -0.5}}))
    assert any("시간 손절 2건" in x for x in f)


def test_facts_flags_unconfirmed_exits():
    """실체결이 안 붙은 청산이 있으면 손익 숫자를 덜 믿어야 한다고 알린다."""
    f = journal.facts(_entry(
        trades={"trades": 2, "wins": 0, "losses": 2, "win_rate": 0.0,
                "pnl_krw": -100, "expectancy_pct": -0.5, "avg_slippage_pct": 0.0},
        by_exit_reason={"stop": {"trades": 2, "expectancy_pct": -0.5}},
        positions=[{"exit_fill_confirmed": 1}, {"exit_fill_confirmed": 0}]))
    assert any("실체결가가 확인되지 않아" in x for x in f)


# --- 실현손익 주값은 계좌다 (사용자 결정 2026-08-03) ---
#
# 건별 실비용 정정 후에도 계좌와 백원대 차이가 남는다(실측 7/31: 건별 −14,426
# vs 계좌 −14,656). 증권사는 이동평균 단가·건별 절사로 계산하고 원장 밖 수동
# 매매도 포함한다 — 일지의 헤드라인은 계좌 확정치를 쓰고 건별 합은 병기한다.

def test_계좌값이_있으면_실현손익_주값은_계좌다(db):
    from app.trade import fills

    ledger.open_position(_order("a1"), fill=10_000)
    ledger.monitor(lambda s: 10_500)
    day = ledger.positions(status="closed")[0]["closed"][:10]
    fills.store_daily(day, {"realized": 777.0, "commission": 10, "tax": 20})

    e = journal.build(day)
    assert e["trades"]["pnl_source"] == "broker"
    assert e["trades"]["pnl_krw"] == 777.0
    assert e["trades"]["engine_pnl_krw"] != 777.0     # 건별 합은 대조로 남는다
    assert e["broker"]["realized"] == 777.0
    assert any("계좌 확정" in f for f in e["facts"])
    assert any("차이" in f and "건별 합" in f for f in e["facts"])


def test_계좌값이_없으면_건별_합이고_그_사실을_밝힌다(db):
    ledger.open_position(_order("a1"), fill=10_000)
    ledger.monitor(lambda s: 10_500)
    day = ledger.positions(status="closed")[0]["closed"][:10]

    e = journal.build(day)
    assert e["trades"]["pnl_source"] == "engine"
    assert any("계좌값 미확보" in f for f in e["facts"])


def test_거래_0건이어도_계좌_실현이_있으면_보인다(db):
    """원장 밖 수동 매매만 있던 날 — '거래 없음' 으로 삼키면 계좌와 어긋난다."""
    from app.trade import fills

    fills.store_daily("2026-07-27", {"realized": -3_000.0})
    e = journal.build("2026-07-27")
    assert e["trades"]["pnl_krw"] == -3_000.0
    assert not any("청산된 거래 없음" in f for f in e["facts"])
