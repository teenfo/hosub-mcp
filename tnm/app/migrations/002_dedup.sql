-- M4: 신규성(novelty) 판정 결과를 raw_items 에 저장 — 분류 워커(M5)가 소비.
-- duplicate 는 analyses(skipped_duplicate) 로 즉시 종결되어 LLM 을 타지 않는다.

alter table tnm_raw_items add column if not exists novelty text;          -- 'new'|'follow_up'|'duplicate'
alter table tnm_raw_items add column if not exists similarity numeric(4,3); -- 판정 근거 최대 코사인

create index if not exists tnm_raw_items_dedup_queue
  on tnm_raw_items (id) where embedding is not null and novelty is null;
