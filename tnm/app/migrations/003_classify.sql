-- M5: reason 환각 검사(원문에 없는 수치) 경고 플래그 — 요청서 5.4
alter table tnm_analyses add column if not exists warn_hallucination boolean not null default false;
