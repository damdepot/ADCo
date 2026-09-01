# KNOWLEDGE_BASE: DATABASE_KNOB_TUNING_METHODS

# POSTGRESQL TUNING STRATEGIES

## 1. PG_SHARED_MEMORY_MANAGEMENT
*   **Engine**: PostgreSQL
*   **Category**: Memory Management
*   **Knobs**: shared_buffers, effective_cache_size, huge_pages
*   **Definition**: Configures the primary caching mechanisms for PostgreSQL. `shared_buffers` dictates how much memory is dedicated to PostgreSQL for caching data, while `effective_cache_size` hints to the query planner about the total available cache (including OS cache).
*   **Objective**: Maximize cache hits for frequently accessed data to minimize disk I/O.
*   **Formulas / Baseline**:
    *   `shared_buffers`: 25% of total system RAM.
    *   `effective_cache_size`: 75% of total system RAM.
    *   `huge_pages`: 'try' or 'on' (if supported by OS).
*   **Conditions**: Universal requirement; especially critical for read-heavy and OLTP workloads.
*   **Restart Requirement**: Static (requires restart) for `shared_buffers` and `huge_pages`; Dynamic for `effective_cache_size`.
*   **Risk & Guardrails**: Setting `shared_buffers` too high (> 40% RAM) can starve the OS cache and lead to out-of-memory (OOM) errors or double-buffering issues.

## 2. PG_PER_QUERY_EXECUTION_MEMORY
*   **Engine**: PostgreSQL
*   **Category**: Execution Memory
*   **Knobs**: work_mem, maintenance_work_mem, autovacuum_work_mem
*   **Definition**: Controls the amount of memory allocated for internal sort operations, hash tables, and maintenance tasks.
*   **Objective**: Prevent disk spills during complex queries (sorts, joins) and speed up maintenance operations like VACUUM and CREATE INDEX.
*   **Formulas / Baseline**:
    *   `work_mem`: (Total RAM * 0.25) / max_connections.
    *   `maintenance_work_mem`: 10% of total RAM, up to 2GB.
    *   `autovacuum_work_mem`: Default to `maintenance_work_mem` or -1.
*   **Conditions**: Important for OLAP, batch processing, or queries with large aggregations and joins.
*   **Restart Requirement**: Dynamic (can be changed on the fly without restart).
*   **Risk & Guardrails**: `work_mem` is per-operation (e.g., a single query can use multiple `work_mem` allocations for multiple sorts). Setting it too high with many connections will cause OOM crashes.

## 3. PG_WAL_CHECKPOINTING_AND_DURABILITY
*   **Engine**: PostgreSQL
*   **Category**: Write-Ahead Logging
*   **Knobs**: checkpoint_completion_target, max_wal_size, min_wal_size, wal_buffers, wal_compression
*   **Definition**: Manages how transactional data is written to disk. Tuning checkpoints spreads write I/O to avoid spikes.
*   **Objective**: Optimize write performance, avoid I/O bottlenecks during checkpoints, and maintain durability guarantees.
*   **Formulas / Baseline**:
    *   `checkpoint_completion_target`: 0.9.
    *   `max_wal_size`: 1GB to 10GB depending on write volume.
    *   `min_wal_size`: 80MB to 1GB.
    *   `wal_buffers`: 16MB (usually sufficient).
    *   `wal_compression`: 'on'.
*   **Conditions**: Write-heavy workloads, bulk data loads, and systems with high transaction rates.
*   **Restart Requirement**: Dynamic for `checkpoint_completion_target` and wal sizes. Static for `wal_buffers`.
*   **Risk & Guardrails**: Large `max_wal_size` increases crash recovery time. Too small causes frequent checkpoint I/O spikes.

## 4. PG_QUERY_PLANNER_AND_IO_CONCURRENCY
*   **Engine**: PostgreSQL
*   **Category**: Planner Configuration
*   **Knobs**: random_page_cost, seq_page_cost, effective_io_concurrency, default_statistics_target
*   **Definition**: Calibrates the cost-based optimizer to the underlying hardware's I/O characteristics and sets statistics gathering depth.
*   **Objective**: Help the optimizer choose index scans over sequential scans on SSDs and improve plan accuracy.
*   **Formulas / Baseline**:
    *   `random_page_cost`: 1.1 for SSD, 4.0 for HDD.
    *   `seq_page_cost`: 1.0.
    *   `effective_io_concurrency`: 200 for SSD, 2 for HDD.
    *   `default_statistics_target`: 100 to 500 (higher for complex queries).
*   **Conditions**: Modern SSD storage; queries picking sub-optimal sequential scans.
*   **Restart Requirement**: Dynamic.
*   **Risk & Guardrails**: Setting `random_page_cost` too low might over-favor index scans even when fetching a large portion of a table, leading to slow performance.

## 5. PG_CONCURRENCY_AND_PARALLEL_WORKERS
*   **Engine**: PostgreSQL
*   **Category**: Concurrency
*   **Knobs**: max_connections, max_worker_processes, max_parallel_workers, max_parallel_workers_per_gather
*   **Definition**: Defines the maximum number of concurrent client connections and parallel execution workers available.
*   **Objective**: Enable parallel execution for analytical queries while capping connections to prevent connection thrashing.
*   **Formulas / Baseline**:
    *   `max_connections`: 100 to 500 (use a connection pooler like PgBouncer for more).
    *   `max_worker_processes`: Equal to total CPU cores.
    *   `max_parallel_workers`: Equal to total CPU cores.
    *   `max_parallel_workers_per_gather`: CPU cores / 2.
*   **Conditions**: Systems with multiple CPU cores and mixed OLTP/OLAP workloads.
*   **Restart Requirement**: Static for `max_connections` and `max_worker_processes`.
*   **Risk & Guardrails**: High `max_connections` wastes shared memory and can lead to context-switching overhead. High parallel workers can monopolize CPUs, starving other queries.

## 6. PG_AUTOVACUUM_BACKGROUND_MAINTENANCE
*   **Engine**: PostgreSQL
*   **Category**: Autovacuum
*   **Knobs**: autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor, autovacuum_vacuum_cost_limit, autovacuum_vacuum_cost_delay
*   **Definition**: Configures the daemon responsible for reclaiming dead tuples and updating table statistics.
*   **Objective**: Prevent table bloat, keep statistics fresh, and avoid autovacuum from consuming all I/O.
*   **Formulas / Baseline**:
    *   `autovacuum_vacuum_scale_factor`: 0.05 (5%) or lower for large tables.
    *   `autovacuum_analyze_scale_factor`: 0.02 (2%).
    *   `autovacuum_vacuum_cost_limit`: 200 to 2000.
    *   `autovacuum_vacuum_cost_delay`: 2ms to 10ms.
*   **Conditions**: Heavy update/delete workloads (MVCC churn).
*   **Restart Requirement**: Dynamic.
*   **Risk & Guardrails**: Tuning too aggressively can consume excessive I/O; tuning too loosely leads to massive bloat and slow queries.

## 7. PG_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY
*   **Engine**: PostgreSQL
*   **Category**: Remediation
*   **Knobs**: shared_buffers, work_mem, max_connections
*   **Definition**: Corrective strategies applied when the database fails to start or crashes due to misconfiguration (e.g., OOM).
*   **Objective**: Recover service availability by stepping down aggressive memory or connection settings.
*   **Formulas / Baseline**:
    *   OOM stepdown: Reduce `work_mem` by 50% if OOM occurs.
    *   Startup crash: Reduce `shared_buffers` to 128MB or 10% of RAM.
    *   Syntax error: Remove unrecognized parameters based on PG version.
*   **Conditions**: System failure, container OOM kill, or startup errors in logs.
*   **Restart Requirement**: Static (often needed to recover).
*   **Risk & Guardrails**: Performance may degrade significantly to ensure stability.


# MYSQL TUNING STRATEGIES

## 8. MYSQL_GLOBAL_BUFFER_POOL_MANAGEMENT
*   **Engine**: MySQL
*   **Category**: Memory Management
*   **Knobs**: innodb_buffer_pool_size, innodb_buffer_pool_instances, innodb_buffer_pool_chunk_size
*   **Definition**: The InnoDB buffer pool caches data and indexes in memory.
*   **Objective**: Minimize disk I/O by fitting the working set into memory.
*   **Formulas / Baseline**:
    *   `innodb_buffer_pool_size`: 60% to 75% of total system RAM.
    *   `innodb_buffer_pool_instances`: 1 per 1GB of buffer pool size (max 8 or 16).
*   **Conditions**: Universal requirement; vital for InnoDB performance.
*   **Restart Requirement**: Dynamic (size can be changed dynamically in modern MySQL versions).
*   **Risk & Guardrails**: Setting size too high leads to OS swapping and OOM crashes. Ensure `innodb_buffer_pool_chunk_size` allows the desired pool size.

## 9. MYSQL_PER_SESSION_BUFFERS_AND_TEMP_TABLES
*   **Engine**: MySQL
*   **Category**: Execution Memory
*   **Knobs**: sort_buffer_size, join_buffer_size, read_rnd_buffer_size, tmp_table_size, max_heap_table_size
*   **Definition**: Buffers allocated on a per-thread or per-operation basis for sorts, joins, and temporary tables.
*   **Objective**: Speed up complex queries and prevent implicit temporary tables from writing to disk.
*   **Formulas / Baseline**:
    *   `sort_buffer_size`: 1MB to 4MB.
    *   `join_buffer_size`: 1MB to 4MB.
    *   `tmp_table_size`: 32MB to 64MB.
    *   `max_heap_table_size`: Equal to `tmp_table_size`.
*   **Conditions**: OLAP, heavy JOINs, ORDER BY, GROUP BY operations.
*   **Restart Requirement**: Dynamic.
*   **Risk & Guardrails**: Session buffers are allocated per thread. Setting them too high with many connections causes OOM. Keep them modest and tune per-query if needed.

## 10. MYSQL_REDO_LOGGING_AND_TRANSACTION_DURABILITY
*   **Engine**: MySQL
*   **Category**: Write-Ahead Logging
*   **Knobs**: innodb_redo_log_capacity, innodb_log_buffer_size, innodb_flush_log_at_trx_commit, innodb_flush_method
*   **Definition**: Configures InnoDB redo log capacity and commit durability policies.
*   **Objective**: Optimize write throughput while maintaining ACID guarantees.
*   **Formulas / Baseline**:
    *   `innodb_redo_log_capacity`: 1GB to 4GB.
    *   `innodb_log_buffer_size`: 16MB.
    *   `innodb_flush_log_at_trx_commit`: 1 (fully ACID) or 2 (better performance, slight data loss risk on OS crash).
    *   `innodb_flush_method`: O_DIRECT (avoids double buffering with OS cache).
*   **Conditions**: Write-heavy transactional workloads.
*   **Restart Requirement**: Dynamic for `innodb_redo_log_capacity`, Static for `innodb_flush_method`.
*   **Risk & Guardrails**: Setting `innodb_flush_log_at_trx_commit=0` or `2` risks up to 1 second of data loss on power failure.

## 11. MYSQL_STORAGE_IO_THREADS_AND_CAPACITY
*   **Engine**: MySQL
*   **Category**: I/O Capacity
*   **Knobs**: innodb_io_capacity, innodb_io_capacity_max, innodb_read_io_threads, innodb_write_io_threads, innodb_adaptive_flushing
*   **Definition**: Calibrates background InnoDB flushing and I/O concurrency to match storage hardware capabilities.
*   **Objective**: Utilize fast SSD I/O to prevent flushing from falling behind during write bursts.
*   **Formulas / Baseline**:
    *   `innodb_io_capacity`: 1000 for standard SSD, higher for NVMe.
    *   `innodb_io_capacity_max`: 2000 for standard SSD.
    *   `innodb_read_io_threads`: 4 to 8.
    *   `innodb_write_io_threads`: 4 to 8.
*   **Conditions**: Fast storage (SSD/NVMe) and write-heavy workloads.
*   **Restart Requirement**: Dynamic for capacities, Static for threads.
*   **Risk & Guardrails**: Setting IO capacity higher than hardware limits can cause high I/O wait and degradation.

## 12. MYSQL_CONNECTIONS_AND_THREAD_CACHING
*   **Engine**: MySQL
*   **Category**: Concurrency
*   **Knobs**: max_connections, thread_cache_size, table_open_cache
*   **Definition**: Manages client connections, thread reuse, and cached file descriptors for tables.
*   **Objective**: Avoid connection refusal errors and reduce overhead of creating new threads and opening tables.
*   **Formulas / Baseline**:
    *   `max_connections`: 151 to 500+.
    *   `thread_cache_size`: 16 to 100 (auto-scaled often).
    *   `table_open_cache`: 2000 to 4000.
*   **Conditions**: High concurrent client applications.
*   **Restart Requirement**: Dynamic.
*   **Risk & Guardrails**: High `max_connections` without adequate RAM leads to OOM. OS file descriptor limits (ulimit) must exceed `table_open_cache`.

## 13. MYSQL_INNODB_PURGE_AND_BACKGROUND_MAINTENANCE
*   **Engine**: MySQL
*   **Category**: Maintenance
*   **Knobs**: innodb_purge_threads, innodb_page_cleaners
*   **Definition**: Configures background threads for purging old row versions and flushing dirty pages.
*   **Objective**: Prevent undo log excessive growth and maintain smooth flushing behavior.
*   **Formulas / Baseline**:
    *   `innodb_purge_threads`: 4.
    *   `innodb_page_cleaners`: 4 (should match buffer pool instances).
*   **Conditions**: High concurrency update/delete workloads.
*   **Restart Requirement**: Static.
*   **Risk & Guardrails**: Creating more page cleaners than buffer pool instances has no effect.

## 14. MYSQL_CHECKER_REMEDIATION_AND_FAILURE_RECOVERY
*   **Engine**: MySQL
*   **Category**: Remediation
*   **Knobs**: innodb_buffer_pool_size, sort_buffer_size, old_passwords
*   **Definition**: Emergency configurations when MySQL fails to start or encounters severe OOM.
*   **Objective**: Restore uptime by safely reducing memory allocations or removing deprecated syntax.
*   **Formulas / Baseline**:
    *   OOM buffer reduction: Reduce `innodb_buffer_pool_size` by 25%.
    *   Deprecated knob: Remove or rename obsolete variables based on MySQL version.
*   **Conditions**: Crash loops, container OOM kills, upgrade failures.
*   **Restart Requirement**: Static.
*   **Risk & Guardrails**: Performance reduction to ensure service stability.
