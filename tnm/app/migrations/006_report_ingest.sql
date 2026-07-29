-- 리포트 → 분석 파이프라인 적재 재시도 횟수.
--
-- 리포트가 raw_items 로 못 들어가는 이유는 여러 가지다: 관심종목 등록 상한에
-- 걸렸거나, 사용자가 제외한 종목이거나, 같은 내용이 이미 있어 중복으로 걸렀거나.
-- 앞의 둘은 다음 사이클에 풀릴 수 있으니 재시도해야 하고, 마지막은 영원히 안
-- 풀리니 멈춰야 한다. 둘을 구분할 방법이 없으므로 raw_items.embed_attempts 와
-- 같은 방식으로 시도 횟수를 세고 상한에서 포기한다.
--
-- 이 값이 있으면 "왜 이 리포트는 분석이 안 됐나" 를 나중에 되짚을 수 있다.
alter table tnm_reports
    add column if not exists ingest_attempts integer not null default 0;

create index if not exists idx_reports_ingest_queue
    on tnm_reports (published_at desc)
    where raw_item_id is null and ticker is not null;
