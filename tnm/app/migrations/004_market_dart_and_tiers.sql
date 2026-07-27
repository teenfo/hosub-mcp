-- 004: DART 전종목 수집 + 뉴스 계층별 수집 주기
--
-- DART list.json 은 corp_code 없이 날짜 범위만 주면 전체 공시를 돌려준다.
-- 종목당 1콜(65콜/사이클)에서 전체 목록 몇 콜로 바꾸면 감시목록 밖 종목까지
-- 같은 비용에 커버된다. 그 전역 커서를 담을 자리가 필요하다.
create table if not exists tnm_collect_state (
    key         text primary key,
    value       text not null,
    updated_at  timestamptz not null default now()
);

-- 뉴스는 반대다. 구글 RSS 는 종목명 검색이라 종목당 1콜이 강제되므로 전종목이
-- 불가능하다. 대신 계층을 나눠 주기를 차등한다 — 매매 대상은 자주, 수집전용은
-- 드물게. tier 는 트레이딩 감시목록의 collect_only 에서 동기화된다.
alter table tnm_watchlist
    add column if not exists tier text not null default 'trade';

-- 소스별 '마지막 시도 시각'. last_collected_at 은 증분 커서(rcept_no 등)라
-- 시각 판정에 쓸 수 없다 — 주기 판정용 타임스탬프를 따로 둔다.
alter table tnm_watchlist
    add column if not exists last_attempt jsonb not null default '{}'::jsonb;

create index if not exists idx_watchlist_tier
    on tnm_watchlist (tier) where is_active and not is_excluded;
