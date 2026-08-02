# 레시피 — 발굴 소스·관측 수집기 추가, scout 수정

## 어느 쪽인가부터 정한다

- **발굴 소스**: 종목을 엔진에 *신호로* 올린다 → 감시목록 편입에 영향
- **관측 수집기**: 원장만 쌓는다 → 판정(4주) 전에는 어디에도 영향 없음

**새 아이디어는 거의 항상 관측 수집기로 시작한다.** 소스 승격은 측정 통과 후.

## 관측 수집기 (기본 경로)

1. 모듈: `trading/app/scout/<이름>.py` (bars_obs.py 가 모범 — API 콜 0 설계)
   - docstring 에 **판정을 미리 못박는다** (필수). 질문 문장만으로는 부족하다 —
     **시점**(예: 4주 뒤), **지표**(무엇과 무엇의 비교), **분할 기준**(상·하위
     반/4분위 등), **표본 제외 규칙**(못 잰 값 처리)까지 적고, "결과를 본 뒤
     기준을 바꾸지 않는다"를 명시한다 (검증 실측 2026-08-02: 질문만 적은
     구현은 판정 시점에 기준을 새로 정하게 된다)
   - 하루 1회 배치면 `job_marks`(store.job_done/mark_job_done)로 완료 영속화
   - 전면 실패 재시도에 상한 (bars_obs 의 3회 상한 패턴)
2. 원장 테이블: `scout/store.py` 에 record_*/rows_* — per-key upsert
   (한 소스 실패가 다른 소스가 넣은 값을 NULL 로 덮지 않게, record_flows 참조)
3. lifespan 배선: `main.py` 의 create_task 목록에 loop 추가
4. 판정 예약: 4주 뒤 트리거(질문은 docstring 그대로)를 세션에서 등록

## 발굴 소스

1. `scout/sources/` 어댑터 — `Source` 프로토콜(enabled/interval_sec/collect)
2. `model.py`: SOURCES 등록, **그룹 배정이 핵심** — 같은 정보원의 변주면 기존
   그룹에 넣는다(그룹 내 max 가 3중 계상을 막는 장치). strength 정규화 규약 준수
3. `observed_at` 은 **정보가 생긴 시각** — 재수집 시각으로 재도장하면 감쇠·TTL
   이 무력해진다(M3 사고)
4. 미검증 소스는 `max_tier=collect` 하드 룰 (presurge 선례)
5. config `scout.<source>` 블록: `enabled` 와 소스별 파라미터, 코드 DEFAULTS 동기

## scout 엔진·게이트 수정 시 주의

- promote.py 의 서킷브레이커들(MAX_SHRINK·max_demote·max_rotate·rotation_ready)을
  우회하는 경로를 만들지 않는다
- probation 은 **완료된 세션** 기준(session_open_before) — 달력일로 되돌리면
  주말 유입이 월요일 개장 즉시 승격된다
- 결정은 전 건 decisions 원장에, `applied` 는 종목별 실제 적용과 일치해야 한다
- 동점 정렬은 일 시드 tie-break — 코드 오름차순 정렬을 넣지 않는다

## 테스트

- 수집기: 파싱 경계(빈 응답·깨진 값·이중부호 "--"), 원장 upsert 멱등,
  실패 격리
- 소스: strength 경계값, TTL, observed_at 고정
- 게이트: shadow 에서 감시목록 쓰기 0건, 서킷브레이커 발동 시나리오
