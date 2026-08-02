# 레시피 — 새 매매 규칙(기법) 추가·수정

레지스트리 패턴이라 다른 코드 수정이 거의 없다. 순서대로:

## 1. 규칙 함수 — `trading/app/signals/rules.py`

```python
@register("규칙이름", side="long")
def my_rule(df, cfg, prev_close=None, now=None):
    ...
    return Signal("규칙이름", "long", entry, stop, target, "사유 문장", ts=...)
```

- 신호 없음이면 None. **현재 봉까지의 데이터만** 사용(미래 참조 금지 —
  백테스트 러너가 같은 함수를 재생하므로 여기서 새면 성적이 거짓이 된다).
- 손절은 구조적 지점 + ATR 버퍼(`_atr_buffer`) — 딱 떨어지는 지지선 바로
  아래는 스탑 헌팅 자리다. 손절폭은 `min_stop_pct~max_stop_pct` 대역 준수.
- 봉 요구량·파라미터는 하드코딩하지 말고 cfg 로 (pullback 의 교훈: 코드에
  박힌 40 때문에 09:40 이전 평가 0건이 설정 어디에도 안 보였다).

## 2. config — `trading/config.yaml` `rules:` 블록

```yaml
  규칙이름:
    enabled: false        # ← 신규 규칙은 반드시 비활성으로 들어온다
    priority: 0.30        # 낮게 — 검증 전 규칙이 검증된 규칙보다 먼저 발주되면 안 된다
    regimes: ["강세", "중립"]   # 해당되면
    # 파라미터마다 근거 주석
```

**신규 규칙은 `enabled: false` 로 편입한다.** 백테스트 리포트·주간 스윕이
비활성 규칙도 성적을 재므로, 측정이 쌓인 뒤 켜는 판단은 사용자가 한다
(divergence 가 이 절차의 선례). UI 토글은 `data/rules.json` 런타임 오버라이드
— config 와 다르면 UI 쪽이 더 최근 사용자 결정이다.

## 3. 진입 시간창 (해당되면)

`rules.entry_cutoff`(전역)·`rules.<규칙>.no_entry`(규칙별)가 이미 있다 —
시간대 근거가 있으면 코드가 아니라 이 설정으로 건다. 게이트는
`rules.entry_window_note()` → signals/engine 게이트 체인이 처리한다.

## 4. 테스트 — `trading/tests/test_<규칙>.py`

- 발동 케이스 / 비발동 케이스 / 경계(봉 부족·워밍업)
- 손절폭 대역 준수, side 확인
- 픽스처 봉 수 ≥ min_bars (60행 미만이면 피처가 서지 않는다)

## 5. 확인할 배선 (자동이지만 눈으로 확인)

- `GET /api/rules` 에 뜨는지 (레지스트리 자동)
- 백테스트 러너·주간 스윕이 집는지 (evaluate_all 경유 — 자동)
- priority 동률이 없는지 — 모든 규칙에 서로 다른 priority (동률이면
  종목코드 순 편중, priority.py 의 존재 이유)

## 흔한 실수

- 신호 차단을 evaluate_all 안에서 하는 것 — 차단은 **엔진 게이트 체인**에서,
  사유(note)와 함께. 백테스트는 게이트 밖이어야 차단 창의 성적을 계속 관측한다.
- 숏 규칙을 만들고 발주를 기대하는 것 — long_only 라 기록만 된다(인버스 매핑은
  orders.propose 가 한다).
