"""켈리 기준 — 지금 이 전략에 자본을 넣어도 되는가.

    f = p - q/b        p 승률 · q 패률(1-p) · b 손익비(평균이익/평균손실)

`f` 는 장기 복리 성장률을 최대화하는 투입 비율이다. **음수면 베팅하지 말라는
뜻**이고, 그건 "조금만 넣어라" 가 아니라 "이 조건에서는 넣을수록 잃는다" 다.

## 왜 이걸 화면에 거나

실측 2026-07-31 (5거래일 107건): 승률 35.5% · 평균이익 +1.302% ·
평균손실 -1.714% → b = 0.759 → **f = -0.494**.

같은 표본으로 이미 알고 있던 것(35개 브래킷 조합이 전부 음수)과 같은 결론인데,
켈리는 그것을 **필요 조건의 숫자**로 바꿔 준다:

    현재 손익비 0.76 유지 시  →  승률 56.8% 가 있어야 한다 (지금 35.5%)
    현재 승률 35.5% 유지 시   →  손익비 1.82 가 있어야 한다 (지금 0.76)

두 번째 줄이 특히 아프다. 설계 목표가 `target_r: 1.5` 인데, **목표에 100%
도달해도 1.82 에 못 미친다.** 목표 설정 자체가 승률과 맞지 않는다.

## 판단에 쓰지 않는다 — 표시만 한다

이 값으로 수량을 자동 조절하지 않는다. 켈리는 승률·손익비가 **안정적일 때**
성립하는 공식인데 우리 표본은 5거래일이고, 그 위에 자동 사이징을 얹으면
노이즈를 레버리지로 증폭한다. 그리고 자금 투입은 사용자의 결정이다.

일지의 사실 목록과 `/api/kelly` 에 싣기만 한다 — 매일 "지금 베팅해도 되는
상태인가" 가 한 줄로 보이게.
"""


def kelly(win_rate: float, avg_win: float, avg_loss: float) -> dict:
    """승률·평균이익·평균손실 → 켈리 지표. 값을 못 내면 `ok=False`.

    `avg_loss` 는 크기(양수)로 받는다. 부호로 받으면 호출부마다 규약이 갈린다.
    """
    p = float(win_rate)
    w, l = float(avg_win), abs(float(avg_loss))
    if not (0 < p < 1) or w <= 0 or l <= 0:
        # 전승·전패이거나 한쪽이 없으면 손익비가 성립하지 않는다. 0 으로
        # 채우면 '계산했는데 0' 과 '못 쟀다' 가 섞인다(이 저장소의 반복 규약).
        return {"ok": False}
    b = w / l
    q = 1.0 - p
    f = p - q / b
    return {
        "ok": True,
        "p": round(p, 4), "b": round(b, 4), "f": round(f, 4),
        # f > 0 이 되려면 무엇이 필요한가 — 목표를 정할 때 쓰는 숫자다
        "need_win_rate": round(1.0 / (1.0 + b), 4),   # 현재 손익비 유지 시
        "need_payoff": round(q / p, 4),               # 현재 승률 유지 시
    }


def from_trades(rows: list[dict], key: str = "pnl_pct") -> dict:
    """청산 기록 묶음 → 켈리 지표. `pnl_pct` 부호로 승패를 가른다.

    본전(0)은 패로 센다 — 왕복 비용을 이미 뺀 값이라 0 은 이익이 아니다.
    """
    pnls = [r.get(key) for r in rows if r.get(key) is not None]
    if len(pnls) < 2:
        return {"ok": False, "n": len(pnls)}
    wins = [x for x in pnls if x > 0]
    loss = [x for x in pnls if x <= 0]
    if not wins or not loss:
        return {"ok": False, "n": len(pnls)}
    out = kelly(len(wins) / len(pnls),
                sum(wins) / len(wins),
                abs(sum(loss) / len(loss)))
    out["n"] = len(pnls)
    out["avg_win"] = round(sum(wins) / len(wins), 4)
    out["avg_loss"] = round(sum(loss) / len(loss), 4)
    # 표본이 몇 **거래일**에 걸쳐 있나 — line() 의 유보 문구가 이 값으로 갈린다.
    # 켈리는 승률·손익비가 안정적일 때 성립하고, 안정성의 단위는 건수가 아니라
    # 날이다(같은 날 20건은 같은 국면의 20표본이다).
    d = {str(r.get("closed") or r.get("d") or "")[:10] for r in rows
         if r.get(key) is not None and (r.get("closed") or r.get("d"))}
    out["days"] = len(d) or None
    return out


def line(k: dict, target_r: float | None = None) -> str:
    """일지 사실 목록에 넣을 한 줄. 값이 없으면 빈 문자열."""
    if not k.get("ok"):
        return ""
    s = (f"[켈리] 표본 {k['n']}건 · 승률 {k['p'] * 100:.1f}% · 손익비 {k['b']:.2f}"
         f" → f = {k['f']:+.3f}")
    if k["f"] <= 0:
        s += " — **음수다. 이 조건에서는 투입할수록 잃는다.**"
    else:
        s += f" (수학적 최적 투입 {k['f'] * 100:.1f}%)"
    s += (f" f>0 이 되려면 승률 {k['need_win_rate'] * 100:.1f}% 또는"
          f" 손익비 {k['need_payoff']:.2f} 가 필요하다")
    if target_r and k["need_payoff"] > target_r:
        s += f" — 설계 목표 {target_r:g}R 로는 도달할 수 없는 값이다"
    # 유보 문구는 **조건부**다. 항상 붙이면 표본이 충분해져도 같은 유보가 붙어
    # 문구의 신호값이 죽는다(감사 2026-08-01 — 종전에는 무조건 붙였다).
    days = k.get("days")
    if days is None:
        s += ". 표본이 20거래일에 못 미치면 판단을 보류한다."
    elif days < 20:
        s += f". 표본이 {days}거래일뿐이다 — 판단을 보류한다."
    else:
        s += f". (표본 {days}거래일)"
    return s
