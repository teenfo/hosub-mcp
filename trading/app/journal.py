"""매매일지 — 하루치 매매 경과를 누적하고 성공·실패 요인을 정리한다.

설계 원칙(TNM 과 동일): **수치·판정은 전부 결정론 코드**가 만든다. LLM 은 이미
계산된 사실을 사람이 읽을 문장으로 엮는 역할만 하고, 매매 판단이나 숫자 계산에는
관여하지 않는다. 게이트웨이가 죽어도 일지는 그대로 만들어진다(요약만 비는 것).

데이터 출처
- 청산 성과: ledger.positions (실제 체결·손익·슬리피지)
- 신호·미발주 사유: signal_log (이 모듈이 만드는 테이블 — 엔진이 매 사이클 기록)
  주문 테이블에는 '발주된 것'만 남아 자금이 없어 밀린 신호가 보이지 않는다.
  자금 배분 문제를 사후에 진단하려면 그 기록이 필요하다.

일지는 DATA_DIR/journal/YYYY-MM-DD.json 으로 남겨 누적 조회한다.
"""
import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "trading.db"
JOURNAL_DIR = Path(settings.DATA_DIR) / "journal"

# 미발주 사유(engine 이 붙이는 note)를 집계용 짧은 라벨로 접는다.
_NOTE_LABELS = (
    ("잔고 부족", "잔고 부족"),
    ("비중 상한", "비중 상한"),
    ("리스크 한도", "리스크 한도"),
    ("최대 동시 포지션", "포지션 한도"),
    ("일일 손실", "일일 가드"),
    ("국면 게이트", "국면 게이트"),
    ("롱 전용", "숏 미발주"),
)


def note_label(note: str) -> str:
    """자유 문장 note → 집계용 라벨. 못 알아보면 '기타'."""
    for needle, label in _NOTE_LABELS:
        if needle in (note or ""):
            return label
    return "기타" if note else "—"


# --------------------------------------------------------------------------
# 신호 기록 (엔진이 호출)
# --------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS signal_log (
            day TEXT NOT NULL, symbol TEXT NOT NULL, rule TEXT NOT NULL,
            ts TEXT, name TEXT, side TEXT,
            entry REAL, stop REAL, target REAL, qty INTEGER, priority REAL,
            actionable INTEGER, note TEXT, order_id TEXT,
            PRIMARY KEY (day, symbol, rule)
        )"""
    )
    return conn


def record_signal(day: str, rec: dict) -> None:
    """엔진이 만든 신호 1건을 기록한다(하루·종목·규칙당 한 행, 최신 상태로 갱신).

    '차단 → 해제'로 상태가 바뀌면 갱신되므로, 하루가 끝났을 때 남는 값이
    그 신호의 최종 결과다.
    """
    with _conn() as conn:
        conn.execute(
            """INSERT INTO signal_log
               (day, symbol, rule, ts, name, side, entry, stop, target,
                qty, priority, actionable, note, order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day, symbol, rule) DO UPDATE SET
                 ts=excluded.ts, qty=excluded.qty, priority=excluded.priority,
                 actionable=excluded.actionable, note=excluded.note,
                 order_id=excluded.order_id""",
            (day, rec.get("symbol", ""), rec.get("rule", ""), rec.get("ts"),
             rec.get("name"), rec.get("side"), rec.get("entry"), rec.get("stop"),
             rec.get("target"), rec.get("qty"), rec.get("priority"),
             1 if rec.get("actionable") else 0, rec.get("note"),
             rec.get("order_id")),
        )


def signals_on(day: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signal_log WHERE day=? ORDER BY ts", (day,)
        ).fetchall()
    return [dict(r) for r in rows]


def last_signal_at(since_days: int = 7) -> dict[str, datetime]:
    """종목별 **마지막 신호 시각**. 발굴 엔진의 '침묵 판정' 입력.

    매매 tier 자리는 신호를 탐지하려고 주는 것이다. 자리를 받고도 신호를 한 건도
    못 내면 그 자리는 놀고 있는 것이고, 대기 후보에게 넘기는 편이 낫다 —
    실측 2026-07-31: 매매 tier 40종목 중 16종목(40%)이 그날 신호 0건이었고,
    매매 tier 밖에서 나온 신호는 0건이었다(자리가 모자라서 놓친 기회는 없었다).

    `actionable` 은 보지 않는다. 자금·한도로 미발주된 것도 **규칙이 걸렸다**는
    뜻이고, 자리의 값어치는 거기서 나온다.
    """
    cutoff = (datetime.now(KST).date() - timedelta(days=since_days)).isoformat()
    out: dict[str, datetime] = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT symbol, MAX(ts) t FROM signal_log WHERE day>=? AND ts IS NOT NULL"
            " GROUP BY symbol", (cutoff,)).fetchall()
    for r in rows:
        try:
            t = datetime.fromisoformat(r["t"])
        except (TypeError, ValueError):
            continue
        out[r["symbol"]] = t if t.tzinfo else t.replace(tzinfo=KST)
    return out


def prune_signals(keep_days: int = 180) -> int:
    """오래된 신호 기록 정리(디스크 방어). 반환: 삭제 행 수."""
    cutoff = (datetime.now(KST).date() - timedelta(days=keep_days)).isoformat()
    with _conn() as conn:
        return conn.execute("DELETE FROM signal_log WHERE day<?", (cutoff,)).rowcount


# --------------------------------------------------------------------------
# 집계 (결정론)
# --------------------------------------------------------------------------
def _hour_of(ts: str) -> int | None:
    m = re.search(r"T(\d{2}):", ts or "")
    return int(m.group(1)) if m else None


def _agg(rows: list[dict]) -> dict:
    """청산된 거래 묶음의 성과. ledger._agg 와 같은 산식(일지 자체 보관용)."""
    if not rows:
        return {"trades": 0, "win_rate": 0.0, "pnl_krw": 0.0,
                "expectancy_pct": 0.0, "wins": 0, "losses": 0}
    pnls = [r.get("pnl_pct") or 0.0 for r in rows]
    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(rows) - len(wins),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "expectancy_pct": round(sum(pnls) / len(pnls), 3),
        "pnl_krw": round(sum(r.get("pnl_krw") or 0 for r in rows), 0),
        "avg_slippage_pct": round(
            sum(r.get("slippage_pct") or 0 for r in rows) / len(rows), 4),
    }


def _by(rows: list[dict], key: str) -> dict:
    out: dict[str, dict] = {}
    for k in sorted({str(r.get(key) or "—") for r in rows}):
        out[k] = _agg([r for r in rows if str(r.get(key) or "—") == k])
    return out


def facts(entry: dict) -> list[str]:
    """관찰 사실 = 성공·실패 요인 후보. 전부 결정론 — 추측 문장을 만들지 않는다.

    LLM 에게는 이 목록만 넘긴다(원자료 해석을 맡기지 않는다).
    """
    out: list[str] = []
    t, sig = entry["trades"], entry["signals"]

    if t["trades"] == 0:
        out.append("청산된 거래 없음 — 성과 판단 불가.")
    else:
        out.append(
            f"청산 {t['trades']}건(승 {t['wins']} · 패 {t['losses']}), "
            f"승률 {t['win_rate']}%, 실현손익 {t['pnl_krw']:,.0f}원, "
            f"건당 기대값 {t['expectancy_pct']:+.2f}%.")

        # 결과를 사유보다 **먼저** 적는다. 사유만 보면 올라간 손절선에서 이익으로
        # 끝난 건까지 손실로 읽힌다(사용자 지침 2026-07-30).
        oc = entry.get("by_outcome") or {}
        if oc:
            out.append("결과: " + " · ".join(
                f"{k} {v['trades']}건({v['pnl_krw']:+,.0f}원)"
                for k, v in oc.items()) + ".")

        reasons = entry["by_exit_reason"]
        parts = [f"{k} {v['trades']}건" for k, v in reasons.items()]
        if parts:
            out.append("청산 사유(기계적 구분 — 결과와 다를 수 있다): "
                       + " · ".join(parts) + ".")
        # 사유는 `stop` 인데 결과는 익절인 건. 라인 추종이 실제로 일한 자리다.
        won_by_stop = [p for p in entry["positions"]
                       if p.get("exit_reason") == "stop" and p.get("outcome") == "익절"]
        if won_by_stop:
            got = sum(float(p.get("pnl_krw") or 0) for p in won_by_stop)
            out.append(
                f"손절선에서 이익으로 끝난 건 {len(won_by_stop)}건({got:+,.0f}원) — "
                "라인 추종이 손절선을 진입가 위로 올린 결과다. 사유는 손절이지만 "
                "익절로 센다.")
        eod = reasons.get("eod", {}).get("trades", 0)
        if eod and eod >= t["trades"] / 2:
            out.append(
                f"마감 정리(eod)가 {eod}건으로 절반 이상 — 목표·손절 어느 쪽에도 "
                "닿지 않고 하루가 끝났다. 목표폭이 장중 변동폭보다 넓거나 진입이 "
                "늦었을 가능성.")

        rules = entry["by_rule"]
        if len(rules) > 1:
            best = max(rules.items(), key=lambda kv: kv[1]["expectancy_pct"])
            worst = min(rules.items(), key=lambda kv: kv[1]["expectancy_pct"])
            out.append(
                f"규칙별: 최고 {best[0]} {best[1]['expectancy_pct']:+.2f}%"
                f"({best[1]['trades']}건) · 최저 {worst[0]} "
                f"{worst[1]['expectancy_pct']:+.2f}%({worst[1]['trades']}건).")

        hours = entry["by_hour"]
        losing = [h for h, v in hours.items() if v["trades"] >= 2
                  and v["expectancy_pct"] < 0]
        if losing:
            out.append("시간대 손실 구간: " + ", ".join(f"{h}시" for h in losing)
                       + " — 해당 시간대 진입 제한(entry_before) 검토 여지.")

        # 시간 손절이 잦으면 셋업이 늦게 잡히거나 보유 상한이 짧다는 신호
        tmo = reasons.get("timeout", {}).get("trades", 0)
        if tmo:
            out.append(
                f"시간 손절 {tmo}건 — 손절·목표 어느 쪽에도 닿지 않아 보유 상한에서 "
                "정리됐다. 진입이 늦었거나 상한이 셋업보다 짧을 수 있다.")
        # 시장가 청산은 이론가(손절선)와 다르게 체결된다. 실측이 안 붙은 건이
        # 많으면 손익 숫자 자체를 덜 믿어야 한다.
        unconfirmed = sum(1 for p in entry["positions"]
                          if not p.get("exit_fill_confirmed"))
        if unconfirmed:
            out.append(
                f"청산 {unconfirmed}건은 실체결가가 확인되지 않아 이론가(손절선·목표가) "
                "기준이다 — 실제 손익은 이보다 나쁠 수 있다.")

        slip = t.get("avg_slippage_pct") or 0
        if abs(slip) >= 0.3:
            out.append(
                f"평균 슬리피지 {slip:+.2f}% — 모델 진입가와 실체결가 괴리가 크다. "
                "시장가 발주 시점이나 호가 유동성 확인 필요.")

    if sig["total"]:
        out.append(
            f"신호 {sig['total']}건 중 발주 {sig['ordered']}건, "
            f"미발주 {sig['blocked']}건.")
        top = sorted(sig["by_note"].items(), key=lambda kv: -kv[1])
        for label, n in top[:3]:
            if label in ("잔고 부족", "비중 상한", "포지션 한도") and n:
                out.append(
                    f"자금·한도로 밀린 신호 {n}건({label}) — 자금 배분 설정"
                    "(종목당 비중·최대 포지션·거래당 리스크)의 직접적 결과.")
            elif n:
                out.append(f"미발주 사유 '{label}' {n}건.")
    else:
        out.append("기록된 신호 없음 — 규칙 조건이 하루 종일 성립하지 않았다.")

    ovr = (entry.get("guard") or {}).get("override")
    if ovr:
        grants = [h for h in ovr.get("history", []) if h.get("mode") != "clear"]
        if grants:
            out.append(
                f"일일 손실 가드를 {len(grants)}회 임시 해제(총 +{ovr.get('extra_loss_pct', 0):g}% 허용). "
                "이 날의 손익은 한도를 열고 낸 결과이므로 다른 날과 같은 기준으로 비교할 수 없다.")
    # 관측치는 사실 목록 맨 뒤에, 관측이라고 못 박아서 넣는다. 요약이 이걸
    # 근거로 매매를 권하지 않도록 문장 안에 유보를 함께 싣는다.
    be = entry.get("breakeven_shadow") or {}
    if be.get("trades"):
        out.append(
            f"[관측] 본전 이동 shadow(목표거리 {be['arm_at_pct']:g}% 무장): "
            f"{be['trades']}건 중 무장 {be['armed']}건 · 가상청산 "
            f"{be['triggered']}건, 실제 대비 {be['delta_krw']:+,.0f}원"
            f"(유리 {be['helped']}건 · 불리 {be['hurt']}건). 매매에 반영되지 않은 "
            "가정치다 — 표본이 20거래일에 못 미치면 판단을 보류한다.")
    # 케이스 원장 누적 — **오늘이 아니라 지금까지**의 사실이다. 하루치 집계만
    # 보면 매일 "표본이 적어 판단 보류" 로 끝나는데, 같은 질문에 대한 답이
    # 이미 누적돼 있다. 진입 후 경로가 이 시스템의 실제 고장 지점이므로
    # 그 값을 매일 눈앞에 둔다.
    cs = entry.get("cases") or {}
    if cs.get("n"):
        # '누적' 이 아니라 **창이 있는 통계**다 — stats() 가 날짜 컷으로
        # 바뀌었으므로(2026-08-01) 문구도 창을 밝힌다.
        win = cs.get("window_days")
        head = f"[최근 {win}일 {cs['n']}건]" if win else f"[누적 {cs['n']}건]"
        line = (f"{head} 진입 후 최대 유리 {cs['mfe_pct']:+.2f}% vs "
                f"최대 불리 {cs['mae_pct']:+.2f}%")
        if cs.get("entry_pos") is not None:
            line += f", 진입가는 직전 30분 범위의 {cs['entry_pos']:.2f} 지점"
        # 분모를 지표마다 따로 적는다 — 목표 도달은 목표가 있는 건만, 손절
        # 관통은 손절폭이 있는 건만 잰다. 하나의 n 으로 나누면 비율이 틀린다.
        line += (f". 한 번도 유리하게 못 간 건 {cs['never_up']}건, "
                 f"목표선에 닿은 {cs['target_reached']}/{cs['target_reached_of']}건 중 "
                 f"목표청산은 {cs['target_exited']}건, 손절선을 관통한 건 "
                 f"{cs['stop_breached']}/{cs['stop_breached_of']}건.")
        out.append(line)
    # 켈리 — "지금 베팅해도 되는 상태인가" 를 한 줄로. 자동 사이징에는 쓰지
    # 않는다(공식이 전제하는 안정성을 5거래일 표본이 갖추지 못한다).
    if entry.get("kelly"):
        from .research import kelly as _kelly
        k = _kelly.line(entry["kelly"],
                        target_r=settings.RULES.get("orb", {}).get("target_r"))
        if k:
            out.append(k)
    # VI-거래 교집합 — `observe.overlap_today` 는 정의만 있고 어디서도 불리지
    # 않아 대조가 실제로는 계산되지 않았다(감사 2026-08-01). 5거래일 실측
    # 교집합 0건: 계속 0이면 VI 는 우리와 무관한 축이고, 0이 아니게 되는 날이
    # 안전 필터를 논할 시점이다 — 그 날을 놓치지 않으려고 매일 일지에서 잰다.
    try:
        from datetime import datetime as _dt

        from .scout import observe
        syms = {p.get("symbol") for p in entry.get("positions", [])
                if p.get("symbol")}
        if syms:
            hit = observe.overlap_today(
                syms, now=_dt.fromisoformat(f"{entry['date']}T12:00:00+09:00"))
            if hit:
                out.append(f"오늘 거래 종목 중 VI 발동 {len(hit)}건: "
                           f"{', '.join(sorted(hit))} — 안전 필터를 논할 시점이다.")
    except Exception:  # noqa: BLE001 - 대조 실패가 일지를 막지 않는다
        log.warning("VI 교집합 대조 실패", exc_info=True)
    if entry.get("carried"):
        out.append(
            f"다음 날로 넘어간 미청산 포지션 {len(entry['carried'])}건 — "
            "장 마감 정리가 실패했거나 자동청산이 꺼져 있었는지 확인.")
    return out


SUMMARY_SYSTEM = """너는 개인 투자자의 매매일지를 정리하는 기록 담당이다.

규칙:
1. 아래 '관찰 사실'은 시스템이 실제 체결 기록에서 계산한 값이다. 이 값만 근거로 쓴다.
2. 숫자를 새로 계산하거나 바꾸지 않는다. 사실에 없는 내용을 지어내지 않는다.
3. 매수·매도 추천, 종목 전망, 목표가 제시를 하지 않는다. 지난 일에 대한 정리만 한다.
4. 근거가 부족하면 "표본이 적어 판단 보류"라고 쓴다.
5. '지난 사례'는 **과거 기록**이다. 오늘의 성과로 세지 말고, 오늘과 같은 패턴이
   반복되는지 확인하는 데만 쓴다. 사례가 오늘 결과와 다르면 그 차이를 적는다.

출력 형식(한국어, 마크다운 없이 평문):
오늘의 결과: (1~2문장)
잘된 점: (없으면 "없음")
아쉬운 점: (없으면 "없음")
내일 점검할 것: (관찰 사실에서 직접 따라오는 것만, 최대 3개)

전체 400자 이내."""


def summary_prompt(entry: dict) -> str:
    lines = [f"날짜: {entry['date']}", "", "관찰 사실:"]
    lines += [f"- {f}" for f in entry["facts"]]
    if entry["positions"]:
        lines += ["", "청산 내역:"]
        for p in entry["positions"][:20]:
            # 결과(익절/손절)를 앞에, 기계적 사유를 괄호에 둔다. 모델이 사유만
            # 보고 "손절 N건" 이라 쓰면 이익으로 끝난 건까지 손실로 요약한다.
            why = p.get("exit_reason") or "?"
            lines.append(
                f"- {p.get('name') or p.get('symbol')} / {p.get('rule')} / "
                f"{p.get('outcome') or '?'}({why}) / {p.get('pnl_pct'):+.2f}%")
    lines += _precedent_lines(entry)
    return "\n".join(lines)


def _precedent_lines(entry: dict) -> list[str]:
    """오늘 쓴 규칙·시간대의 **지난** 케이스를 선례로 붙인다.

    요약이 매일 그날 숫자만 보고 "표본이 적어 판단 보류" 로 끝나는 것을 막는다.
    같은 규칙을 같은 시간대에 몇 번이나 돌렸고 그때 무엇이 그러했는지가 이미
    원장에 있다.

    **오늘 케이스는 제외한다**(`exclude_day`). 오늘 거래를 오늘의 선례로 인용하면
    같은 사실을 두 번 세는 것이고, 요약이 그걸 독립된 근거처럼 읽는다.
    """
    if not entry.get("positions"):
        return []
    try:
        from .research import cases
    except Exception:  # noqa: BLE001
        return []
    # 오늘 가장 많이 쓴 (규칙, 시간대) 두 조합만 — 프롬프트가 길어지면 요약이
    # 사실 목록보다 선례를 따라간다.
    combos = Counter((p.get("rule"), _hour_of(p.get("opened")))
                     for p in entry["positions"])
    lines: list[str] = []
    for (rule, hour), _ in combos.most_common(2):
        if not rule:
            continue
        try:
            rows = cases.similar(rule, hour, limit=3, exclude_day=entry["date"])
        except Exception:  # noqa: BLE001 - 선례 조회 실패가 요약을 막지 않는다
            log.warning("선례 조회 실패 %s", rule, exc_info=True)
            continue
        if not rows:
            continue
        lines.append("")
        lines.append(f"지난 사례({rule} · {hour}시대, 참고용):")
        for c in rows:
            note = " / ".join(c.get("lessons") or []) or "특이사항 없음"
            lines.append(
                f"- {c['d']} {c.get('name') or c.get('symbol')} "
                f"{(c.get('pnl_pct') or 0):+.2f}% — {note}")
    return lines


# --------------------------------------------------------------------------
# 일지 생성·보관
# --------------------------------------------------------------------------
def _path(day: str) -> Path:
    return JOURNAL_DIR / f"{day}.json"


def build(day: str | None = None) -> dict:
    """하루치 일지를 계산한다(LLM 요약 제외 — 순수 함수에 가깝게 유지)."""
    from .trade import ledger, override

    from .trade import breakeven

    day = day or datetime.now(KST).date().isoformat()
    closed = [p for p in ledger.positions(status="closed", limit=500)
              if str(p.get("closed") or "")[:10] == day]
    carried = [p for p in ledger.positions(status="open", limit=200)
               if str(p.get("opened") or "")[:10] <= day]
    sigs = signals_on(day)
    ordered = [s for s in sigs if s.get("actionable")]
    blocked = [s for s in sigs if not s.get("actionable")]

    entry = {
        "date": day,
        "generated": datetime.now(KST).isoformat(timespec="seconds"),
        "trades": _agg(closed),
        "by_rule": _by(closed, "rule"),
        "by_exit_reason": _by(closed, "exit_reason"),
        # 결과 구분(익절/손절/본전)은 **실제 손익 부호**로 정한다 — 사유가 `stop`
        # 이어도 이익이면 익절이다(사용자 지침 2026-07-30, ledger.outcome 참조).
        # `by_exit_reason` 을 대체하지 않고 나란히 둔다: 사유는 무엇을 고칠지,
        # 결과는 그날 벌었는지를 답하는 서로 다른 질문이다.
        "by_outcome": _by(closed, "outcome"),
        "by_hour": {str(h): _agg([r for r in closed if _hour_of(r.get("opened")) == h])
                    for h in sorted({_hour_of(r.get("opened")) for r in closed}
                                    - {None})},
        "positions": closed,
        "carried": carried,
        "signals": {
            "total": len(sigs),
            "ordered": len(ordered),
            "blocked": len(blocked),
            "by_note": dict(Counter(note_label(s.get("note") or "")
                                    for s in blocked)),
            "by_rule": dict(Counter(s.get("rule") or "—" for s in sigs)),
            "rows": sigs,
        },
        # 그날 손실 가드를 임시로 열었다면 그 사실을 함께 남긴다 — 성과를 읽을 때
        # '한도를 열고 낸 결과'인지 아닌지가 해석을 바꾼다.
        "guard": {"override": override.record_for(day)} if override.record_for(day) else {},
        # 본전 이동 shadow — **관측치**다. 성과 집계(trades/by_rule)에는 섞지
        # 않는다. 20거래일이 쌓이기 전에는 어느 쪽이 나은지 모른다.
        "breakeven_shadow": breakeven.report(day),
        "summary": {},
    }
    # 케이스 원장 — 오늘 청산분을 사후 측정치와 함께 남기고, 누적 요약을 싣는다.
    # 일지가 매일 '오늘의 집계'만 보던 것을 **누적된 사실 위에서** 읽게 하려는
    # 것이다. 실패해도 일지는 만든다 — 관측이 기록을 막을 이유가 없다.
    try:
        from .research import cases

        cases.build_day(day)
        entry["cases"] = cases.stats()
    except Exception:  # noqa: BLE001
        log.warning("케이스 적재 실패 — 일지는 그대로 만든다", exc_info=True)
        entry["cases"] = {}
    # 켈리 — **누적** 표본으로 낸다. 하루치로 내면 승률 0% 또는 100% 인 날이
    # 흔해 손익비가 성립하지 않고, 성립하는 날만 값이 찍혀 그 자체가 편향이 된다.
    try:
        from .research import kelly as _kelly

        entry["kelly"] = _kelly.from_trades(
            ledger.positions(status="closed", limit=500))
    except Exception:  # noqa: BLE001
        log.warning("켈리 계산 실패", exc_info=True)
        entry["kelly"] = {}
    entry["facts"] = facts(entry)
    return entry


def save(entry: dict) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(entry["date"])
    p.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    return p


def load(day: str) -> dict | None:
    try:
        return json.loads(_path(day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def history(days: int = 30) -> list[dict]:
    """최근 일지들의 요약 행(누적 조회용). 최신순."""
    out = []
    if not JOURNAL_DIR.exists():
        return out
    for p in sorted(JOURNAL_DIR.glob("*.json"), reverse=True)[:days]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = d.get("trades", {})
        out.append({
            "date": d.get("date", p.stem),
            "trades": t.get("trades", 0),
            "win_rate": t.get("win_rate", 0.0),
            "pnl_krw": t.get("pnl_krw", 0.0),
            "expectancy_pct": t.get("expectancy_pct", 0.0),
            "signals": (d.get("signals") or {}).get("total", 0),
            "blocked": (d.get("signals") or {}).get("blocked", 0),
            "has_summary": bool((d.get("summary") or {}).get("text")),
            "final": bool(d.get("final")),      # 장 마감 후 확정본인가
        })
    return out


def cumulative(rows: list[dict]) -> dict:
    """기간 누적 — 일자 요약을 합산한다(거래 없는 날은 승률 평균에서 제외)."""
    traded = [r for r in rows if r["trades"]]
    n = sum(r["trades"] for r in traded)
    if not n:
        return {"days": len(rows), "trading_days": 0, "trades": 0,
                "pnl_krw": 0.0, "win_rate": 0.0, "expectancy_pct": 0.0,
                "best_day": None, "worst_day": None}
    wins = sum(r["win_rate"] / 100 * r["trades"] for r in traded)
    return {
        "days": len(rows),
        "trading_days": len(traded),
        "trades": n,
        "pnl_krw": round(sum(r["pnl_krw"] for r in traded), 0),
        "win_rate": round(wins / n * 100, 1),
        "expectancy_pct": round(
            sum(r["expectancy_pct"] * r["trades"] for r in traded) / n, 3),
        "best_day": max(traded, key=lambda r: r["pnl_krw"])["date"],
        "worst_day": min(traded, key=lambda r: r["pnl_krw"])["date"],
    }


def summary_ready(day: str, now: datetime | None = None) -> tuple[bool, str]:
    """그날 요약을 써도 되는가 — 장이 끝나야 하루의 결과가 확정된다.

    장중 요약은 '오전까지의 손실'을 하루의 결론처럼 쓰게 되어 오해를 부른다.
    지난 날짜는 이미 끝났으므로 언제든 쓸 수 있다.
    반환: (가능 여부, 불가 사유)
    """
    now = now or datetime.now(KST)
    today = now.date().isoformat()
    if day < today:
        return True, ""
    if day > today:
        return False, "아직 오지 않은 날짜입니다"
    cutoff = str(settings.CONFIG.get("journal", {}).get("run_after", "15:45"))
    if now.strftime("%H:%M") < cutoff:
        return False, f"장 마감 후({cutoff} 이후) 자동 작성됩니다 — 지금은 집계만 표시"
    return True, ""


async def summarize(entry: dict) -> dict:
    """관찰 사실을 사람이 읽을 문장으로 엮는다(llm-gateway 경유).

    실패는 일지 생성을 막지 않는다 — 사유만 기록하고 빈 요약을 돌려준다.
    """
    try:
        from .llmgw import AsyncLLMGateway, GatewayError
    except ImportError as e:      # pragma: no cover - 배포 누락 방어
        return {"ok": False, "error": f"클라이언트 없음: {e}"}
    cfg = settings.CONFIG.get("journal", {})
    role = cfg.get("llm_role", "general")
    # 프록시 타임아웃(180초)보다 짧게 잡아, 먼저 포기하고 사유를 남기게 한다.
    wait = float(cfg.get("llm_timeout_sec", 150))
    try:
        gw = AsyncLLMGateway()
        text = await gw.run(role, summary_prompt(entry),
                            system=SUMMARY_SYSTEM, timeout=wait)
    except GatewayError as e:
        log.warning("일지 요약 실패(%s): %s", type(e).__name__, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "role": role}
    except Exception as e:  # noqa: BLE001 - 요약 실패로 일지를 막지 않는다
        log.exception("일지 요약 오류")
        return {"ok": False, "error": str(e), "role": role}
    return {"ok": True, "text": text.strip(), "role": role,
            "at": datetime.now(KST).isoformat(timespec="seconds")}


async def _reconcile_first() -> bool:
    """집계 전에 증권사 체결내역으로 실측가·실비용을 확정한다.

    원장의 진입·청산가는 WS 스냅샷과 이론가(손절선·목표가)에서 오고, 비용은 모델
    공식이다. 증권사 실비용이 붙어야 일지의 숫자가 계좌와 맞는다 — 2026-07-29 실측에서
    대사 전 −12,238원이 대사 후 −10,442원이 됐고(증권사 −10,620원과 178원 차이),
    9시대 성과는 부호까지 뒤집혔다.

    `reconcile_executions` 는 멱등이라 여러 번 돌려도 비용이 겹치지 않는다.
    실패해도 일지 생성을 막지 않는다 — 사실 목록에 그 사실만 남긴다.
    """
    import asyncio

    from .kiwoom.client import client
    from .kiwoom.realized import parse_executions
    from .trade import ledger

    if not settings.KIWOOM_APP_KEY:
        return False
    try:
        execs = parse_executions(await client.executions())
        await asyncio.to_thread(ledger.reconcile_executions, execs)
        return True
    except Exception:  # noqa: BLE001 - 대사 실패가 일지를 막지 않는다
        log.warning("일지 대사 실패 — 모델 비용 기준으로 집계한다", exc_info=True)
        return False


async def generate(day: str | None = None, with_summary: bool = True) -> dict:
    """일지 생성 + 저장. 하루 마감 잡과 수동 재생성이 함께 쓴다.

    장이 끝나기 전에는 요약을 만들지 않는다(집계만 저장). final 이 False 인 저장본은
    '진행 중 스냅샷'이라, 조회할 때 저장본 대신 그때그때 다시 계산한다.
    """
    reconciled = await _reconcile_first()
    entry = build(day)
    # 대사에 실패했으면 숫자가 모델 비용 기준이라는 사실을 일지에 남긴다.
    # 조용히 모델값을 쓰면 그게 실측인 줄 알고 읽게 된다.
    if not reconciled:
        entry["facts"].append(
            "[주의] 증권사 체결내역 대사에 실패해 손익이 모델 비용 기준이다 — "
            "실제 값과 다를 수 있다(2026-07-29 실측: 대사 전후 1,796원 차이).")
    ok, why = summary_ready(entry["date"])
    entry["final"] = ok
    if with_summary and ok:
        entry["summary"] = await summarize(entry)
    elif with_summary:
        entry["summary"] = {"ok": False, "pending": True, "reason": why}
    save(entry)
    return entry


async def loop() -> None:
    """평일 장 마감 후 1회 일지 작성.

    완료 표시는 디스크(`job_marks`)에 남긴다 — 메모리 플래그였을 때는
    15:45 이후 재배포마다 LLM 요약이 다시 돌았다(배포 5회 = 요약 5회,
    eod_backfill 이 겪은 것과 같은 결함, 감사 2026-08-01 확인).
    """
    import asyncio

    from .data import store as bars_store

    while True:
        try:
            cfg = settings.CONFIG.get("journal", {})
            now = datetime.now(KST)
            today = now.date().isoformat()
            if (
                cfg.get("enabled", True)
                and now.weekday() < 5
                and now.strftime("%H:%M") >= cfg.get("run_after", "15:45")
                and not await asyncio.to_thread(
                    bars_store.job_done, "journal", today)
            ):
                entry = await generate(today)
                await asyncio.to_thread(bars_store.mark_job_done, "journal", today)
                log.info("매매일지 작성 %s — 청산 %d건 · 신호 %d건 · 요약 %s",
                         today, entry["trades"]["trades"],
                         entry["signals"]["total"],
                         "성공" if entry["summary"].get("ok") else "실패")
                prune_signals(int(cfg.get("signal_keep_days", 180)))
        except Exception:  # noqa: BLE001 - 일지 실패가 서비스를 멈추지 않는다
            log.exception("매매일지 작성 오류")
        await asyncio.sleep(300)
