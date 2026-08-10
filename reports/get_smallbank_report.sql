-- SQLite
select 
    r.id as run_id,
    r.sandbox_id as sandbox_id,
    datetime(r.timestamp, '+7 hours') as rewriter_run_timestamp,
    r.model as rewriter_model,
    r.run_status as rewriter_run_status,
    r.total_duration_ms / 1000.0 as rewriter_total_duration_s,
    r.total_input_tokens as rewriter_total_input_tokens,
    r.total_output_tokens as rewriter_total_output_tokens,
    c.model as checker_model,
    c.run_status as checker_run_status,
    c.total_duration_ms / 1000.0 as checker_duration_s,
    c.checker_status as checker_status,
    c.summary as checker_summary,
    t.run_status as smallbank_run_status,
    t.duration_ms / 1000.0 as smallbank_duration_s,
    t.total_executed as smallbank_total_executed,
    t.total_tps as smallbank_total_tps
from rewriter_runs r
left join checker_runs c on r.sandbox_id = c.sandbox_id
left join smallbank_runs t on r.sandbox_id = t.sandbox_id
where r.run_status = 'success';