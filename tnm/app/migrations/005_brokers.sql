-- 증권사 리서치 리포트 수집 (2026-07-29)
--
-- 뉴스·공시와 성격이 다르다. 뉴스는 '무슨 일이 있었나'고 리포트는 '전문가가
-- 어떻게 보나'다. 목표주가·투자의견은 뉴스에 없는 정보이고, 같은 종목에 대한
-- 증권사 간 시각차는 그 자체가 신호다.
--
-- 종목에 붙는 tnm_watchlist 와 달리 **증권사에 붙는다** — 수집 단위가 종목이
-- 아니라 리서치 목록이기 때문이다. 그래서 별도 테이블을 둔다.
-- config.yaml 이 아니라 DB 에 두는 이유: 수집 토글은 사용자가 장중에 바꾸는
-- 운영 데이터다. config 에 두면 배포해야 바뀐다.
create table if not exists tnm_brokers (
    id            bigserial primary key,
    name          text not null unique,       -- 증권사명 (리포트에 적힌 표기 그대로)
    kind          text not null default 'domestic',  -- domestic / foreign
    enabled       boolean not null default true,     -- 수집 여부 토글
    -- 표기 흔들림 흡수. 같은 회사가 소스마다 '유안타증권'/'유안타 리서치' 처럼
    -- 다르게 적힌다. 매칭은 이 별칭까지 포함해서 한다.
    aliases       text[] not null default '{}',
    note          text,
    reports       integer not null default 0,        -- 누적 수집 건수(화면 표시용)
    last_seen_at  timestamptz,                       -- 이 증권사 리포트를 마지막으로 본 시각
    created_at    timestamptz not null default now()
);

create index if not exists idx_brokers_enabled
    on tnm_brokers (enabled) where enabled;

-- 리포트 원장. raw_items 를 쓰지 않는 이유: raw_items 는 watchlist_id 가 필수인데
-- 산업분석·시황 리포트에는 종목이 없고, 관심종목 밖 종목의 리포트도 남겨야
-- "어느 증권사가 무엇을 보는가" 를 잴 수 있다. 그리고 목표주가·투자의견은
-- 뉴스에 없는 칼럼이라 억지로 끼워 넣으면 둘 다 읽기 어려워진다.
--
-- 관심종목에 해당하는 종목분석 리포트는 raw_items 에도 **함께** 적재해 기존
-- 임베딩→중복제거→분류→알림 파이프라인이 그대로 분석한다(raw_item_id 로 연결).
create table if not exists tnm_reports (
    id            bigserial primary key,
    source        text not null default 'naver',  -- naver / consensus / news
    source_uid    text not null,              -- 네이버 리서치 nid 등 소스 내 고유값
    broker        text not null,              -- tnm_brokers.name (외래키를 걸지 않는다 — 아래)
    category      text not null,              -- company / industry / market / economy / invest
    ticker        text,                       -- 종목분석만 있음. 나머지는 null
    stock_name    text,
    title         text not null,
    summary       text,
    url           text not null,
    pdf_url       text,
    analyst       text,
    target_price  bigint,                     -- 목표주가(원). 파싱 실패·미기재면 null
    opinion       text,                       -- 투자의견 원문 (매수/BUY/Overweight …)
    published_at  timestamptz not null,
    raw_item_id   bigint references tnm_raw_items(id) on delete set null,
    collected_at  timestamptz not null default now(),
    unique (source, source_uid)
);

-- 외래키를 걸지 않는 이유: 목록에 없는 증권사 리포트가 먼저 들어올 수 있다.
-- 그때 수집을 실패시키는 것보다 **'미등록 증권사'로 남겨 목록에 추가하도록
-- 보여주는 것**이 낫다 — 증권사 목록은 사람이 관리하는 것이고, 놓친 이름이
-- 있다는 사실 자체가 정보다.
create index if not exists idx_reports_published on tnm_reports (published_at desc);
create index if not exists idx_reports_broker on tnm_reports (broker, published_at desc);
create index if not exists idx_reports_ticker on tnm_reports (ticker, published_at desc)
    where ticker is not null;
