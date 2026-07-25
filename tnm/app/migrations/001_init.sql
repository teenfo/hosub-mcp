-- TNM 초기 스키마 — 요청서 4장 + 운영 확장 컬럼.
-- 마이그레이션 러너(db.py)가 schema_migrations 기준으로 1회만 적용한다.

create extension if not exists vector;

-- 관심종목 (자동 동기: 트레이딩 감시목록·보유계좌 / 수동 등록, 사용자 편집 가능)
create table if not exists tnm_watchlist (
  id              bigserial primary key,
  ticker          text not null unique,            -- 예: '298040'
  name            text not null,
  dart_corp_code  text,
  score_threshold int not null default 60,
  daily_alert_cap int not null default 5,
  is_active       boolean not null default true,
  -- 확장: 자동 동기와 수동 편집의 공존 규칙
  origin          text not null default 'manual',  -- 'trading' | 'holding' | 'manual'
  is_excluded     boolean not null default false,  -- 사용자가 제외 — 동기화가 되살리지 않음
  last_seen_at    timestamptz,                     -- 자동 동기 소스에서 마지막 관측
  last_collected_at jsonb not null default '{}',   -- 소스별 증분 커서 {"dart": rcept_no, "news": iso}
  created_at      timestamptz not null default now()
);

-- 수집 원문 (원문 보존 — 요청서 P5)
create table if not exists tnm_raw_items (
  id            bigserial primary key,
  watchlist_id  bigint not null references tnm_watchlist(id),
  source        text not null,                     -- 'dart' | 'naver' | 'rss'
  source_uid    text not null,                     -- rcept_no 또는 URL 해시
  title         text not null,
  body          text,
  url           text not null,
  published_at  timestamptz not null,
  content_hash  char(64) not null,
  embedding     vector(1024),                      -- bge-m3 (NULL = 임베딩 대기 큐)
  norm_body     text,                              -- 정규화 본문 (해시 산출 원천)
  embed_attempts int not null default 0,           -- Mac 다운 시 지수백오프용
  next_retry_at timestamptz,
  collected_at  timestamptz not null default now(),
  unique (source, source_uid)
);

create index if not exists tnm_raw_items_watch_pub
  on tnm_raw_items (watchlist_id, published_at desc);
create index if not exists tnm_raw_items_embedding
  on tnm_raw_items using hnsw (embedding vector_cosine_ops);
create index if not exists tnm_raw_items_embed_queue
  on tnm_raw_items (collected_at) where embedding is null;

-- LLM 판정 + 스코어
create table if not exists tnm_analyses (
  id              bigserial primary key,
  raw_item_id     bigint not null unique references tnm_raw_items(id),
  status          text not null,                   -- 'ok' | 'llm_failed' | 'skipped_duplicate'
  category        text,
  is_material     boolean,
  impact_direction text,                           -- 'positive' | 'negative' | 'neutral' | 'unclear'
  impact_horizon  text,                            -- 'immediate' | 'short' | 'long'
  confidence      numeric(3,2),
  novelty         text,                            -- 'new' | 'follow_up' | 'duplicate'
  reason          text,
  summary         text,
  score           int,
  score_detail    jsonb,                           -- 산식 각 항 값 (사후 추적 — 요청서 6장)
  model_name      text,
  latency_ms      int,
  retries         int not null default 0,
  input_hash      char(64),
  created_at      timestamptz not null default now()
);

create index if not exists tnm_analyses_score
  on tnm_analyses (score desc, created_at desc);

-- 알림 이력
create table if not exists tnm_notifications (
  id            bigserial primary key,
  analysis_id   bigint not null references tnm_analyses(id),
  channel       text not null,                     -- 'slack'
  sent_at       timestamptz not null default now(),
  is_deferred   boolean not null default false
);

-- 사람 라벨 (Shadow 모드 검증용)
create table if not exists tnm_labels (
  id            bigserial primary key,
  analysis_id   bigint not null unique references tnm_analyses(id),
  human_verdict text not null,                     -- 'important' | 'noise'
  note          text,
  labeled_at    timestamptz not null default now()
);

-- LLM 호출 로그 (비기능 9장: 입력해시·모델·지연·재시도 기록)
create table if not exists tnm_llm_calls (
  id            bigserial primary key,
  raw_item_id   bigint not null references tnm_raw_items(id),
  input_hash    char(64),
  model_name    text,
  latency_ms    int,
  attempt       int not null default 1,
  ok            boolean not null,
  error         text,
  called_at     timestamptz not null default now()
);
