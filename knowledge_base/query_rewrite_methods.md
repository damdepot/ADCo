# KNOWLEDGE_BASE: DATABASE_QUERY_REWRITE_AND_OPTIMIZATION

## 1. COMBINING_QUERIES
*   **Definition**: Merging multiple isolated or sequential queries into a unified execution plan.
*   **Objective**: Minimize application-to-database network round-trips; maximize global query optimization visibility.
*   **Mechanisms**:
    *   Consolidating linear Common Table Expressions (CTEs) or nested subqueries into single-pass table scans.
    *   Replacing downstream procedural loops with declarative set operations (`UNION ALL`, multi-key joins).
    *   Materializing intermediate results to avoid redundant re-fetches.

## 2. PREDICATE_PUSHDOWN
*   **Definition**: Filtering data at the earliest possible stage in the execution pipeline, typically at the storage engine or data-source layer.
*   **Objective**: Minimize disk I/O, reduce memory consumption, and prevent unnecessary network payload transfer during joins/shuffles.
*   **Mechanisms**: Evaluating `WHERE` filter clauses during table/index scans before the data is emitted to joining or aggregating operators. Use derived tables or subqueries to materialize only the qualifying rows before joining with larger tables.

## 3. JOIN_ORDER_HINTS
*   **Definition**: Explicit developer directives embedded within a query to override the default cost-based optimizer (CBO) execution path.
*   **Objective**: Correct suboptimal query plans caused by stale, missing, or inaccurate database catalog statistics.
*   **Mechanisms**: Passing database-specific syntactic tokens (e.g., SQL comments like `/*+ HINT() */`, `STRAIGHT_JOIN`, or framework API methods) to force explicit join strategies (e.g., broadcast hash join vs. shuffle sort-merge join). Use hints sparingly — only when the optimizer consistently picks a bad plan.

## 4. SEPARATING_QUERIES
*   **Definition**: The intentional deconstruction of a highly complex, monolithic query into smaller, isolated intermediate steps.
*   **Objective**: Prevent resource exhaustion (OOM errors, disk spilling) and eliminate optimizer bottlenecks on deeply nested execution trees.
*   **Mechanisms**:
    *   Materializing massive multi-join intermediate results into temporary tables or materialized views.
    *   Implementing windowed query splitting (chunking batch operations chronologically or by ID ranges) to alleviate long-lived transactional locks.
    *   Separating correlated subqueries into standalone batch reads before executing batch writes.

## 5. CONCURRENCY
*   **Definition**: Structuring queries or application workloads to maximize parallel hardware execution.
*   **Objective**: Maximize horizontal/vertical scaling to decrease overall latency and elevate transaction throughput.
*   **Mechanisms**:
    *   **Intra-query Parallelism**: Rewriting queries to allow the engine to partition data blocks across separate CPU cores or cluster worker nodes.
    *   **Decoupled Execution**: Splitting single-thread sequential code blocks into topologically independent queries that run asynchronously when free of data-lineage dependencies.
    *   **Loop Batching**: Replacing N individual per-item queries with set-based `IN (...)` batch queries and batch write APIs (`executemany` or equivalent), reducing N-1 round-trips.

---
# INTRODUCTION TO CALCITE REWRITE STRATEGIES

Apache Calcite provides a comprehensive rule-based optimizer with over 50 rewrite rules spanning multiple optimization categories. These rules operate on Calcite's internal relational algebra representation (`RelNode`) and can be combined into custom optimization programs. Below is a categorized reference of Calcite's rewrite strategies with their conditions and transformations.

## 6. AGGREGATE_TRANSFORMATION_STRATEGIES

### 6.1. AGGREGATE_MERGE
*   **Definition**: Combine nested `GROUP BY` operations where the outer group keys are a subset of the inner group keys.
*   **Objective**: Eliminate redundant aggregation layers by collapsing two-pass aggregations into a single pass.
*   **Conditions**: Outer `GROUP BY` keys are a subset of inner `GROUP BY` keys; aggregate functions are merge-compatible (SUM, MIN, MAX, COUNT — not AVG or DISTINCT aggregates).
*   **Mechanisms**: Merge the two `GROUP BY` operations into one using the inner (wider) set of group keys, adapting aggregate functions accordingly. For non-mergeable functions (AVG of AVGs, COUNT of COUNTs), the nested structure is preserved.

### 6.2. AGGREGATE_PROJECT_MERGE
*   **Definition**: Eliminate redundant projection between an aggregate and its input.
*   **Objective**: Remove unnecessary intermediate column transformations when the projection does not alter the grouping or aggregate semantics.
*   **Conditions**: `GROUP BY` uses simple columns (no expressions); aggregate functions operate directly on columns; outer SELECT merely renames/passthroughs columns.
*   **Mechanisms**: Direct the `GROUP BY` and aggregates at the underlying data source, removing the passthrough projection layer.

### 6.3. AGGREGATE_EXPAND_DISTINCT_AGGREGATES
*   **Definition**: Rewrite `COUNT(DISTINCT x)`, `SUM(DISTINCT x)`, etc., to equivalent non-DISTINCT aggregates over properly grouped subqueries.
*   **Objective**: Enable standard aggregation operators to handle DISTINCT semantics without special DISTINCT-aware aggregate implementations.
*   **Conditions**: Presence of DISTINCT aggregates (`COUNT(DISTINCT x)`, `SUM(DISTINCT x)`, `MIN(DISTINCT x)`, `MAX(DISTINCT x)`).
*   **Mechanisms**:
    *   Single column, all DISTINCT → `GROUP BY x` with non-DISTINCT aggregates.
    *   Mixed DISTINCT/non-DISTINCT or different DISTINCT arguments → split into separate `GROUP BY` subqueries joined together.

### 6.4. AGGREGATE_EXPAND_DISTINCT_AGGREGATES_TO_JOIN
*   **Definition**: Decompose queries with multiple DISTINCT aggregates over different columns into join-based plans.
*   **Objective**: Handle complex DISTINCT aggregate combinations without dedicated multi-DISTINCT aggregate support.
*   **Conditions**: One or more DISTINCT aggregate functions on different columns.
*   **Mechanisms**: Decompose into multiple intermediate `GROUP BY` subqueries (one per DISTINCT column), then JOIN them on the common grouping columns.

### 6.5. AGGREGATE_REDUCE_FUNCTIONS
*   **Definition**: Decompose complex aggregate functions into simpler primitives.
*   **Objective**: Reduce the set of aggregate functions an engine must implement natively.
*   **Mechanisms**:
    *   `AVG(x)` → `SUM(x) / COUNT(x)`
    *   `STDDEV_POP(x)` → `SQRT((SUM(x^2) - SUM(x)^2/COUNT(x)) / COUNT(x))`
    *   `STDDEV_SAMP(x)` → `SQRT((SUM(x^2) - SUM(x)^2/COUNT(x)) / (COUNT(x)-1))`
    *   `VAR_POP(x)` → `(SUM(x^2) - SUM(x)^2/COUNT(x)) / COUNT(x)`
    *   `VAR_SAMP(x)` → `(SUM(x^2) - SUM(x)^2/COUNT(x)) / (COUNT(x)-1)`
    *   `COVAR_POP/COVAR_SAMP/REGR_SXX/REGR_SYY` → expressions of SUM and COUNT.

### 6.6. AGGREGATE_CASE_TO_FILTER
*   **Definition**: Convert `AGG(CASE WHEN <cond> THEN <expr> END)` to `AGG(<expr>) FILTER (WHERE <cond>)`.
*   **Objective**: Enable more efficient filter-based aggregation execution and further filter pushdown.
*   **Conditions**: Aggregate function wraps a `CASE WHEN <cond> THEN <expr> END` expression exclusively.
*   **Mechanisms**: Replace the CASE expression with a `FILTER (WHERE <cond>)` clause on the aggregate function.

### 6.7. AGGREGATE_FILTER_TRANSPOSE
*   **Definition**: Push filter conditions on grouping columns below the aggregation (convert HAVING on GROUP BY columns to WHERE on the input).
*   **Objective**: Reduce rows before aggregation when filters reference only group key columns.
*   **Conditions**: WHERE/HAVING conditions reference only columns in the GROUP BY clause.
*   **Mechanisms**: Move HAVING conditions referencing only GROUP BY columns into a WHERE clause on a subquery that feeds the aggregation.

### 6.8. AGGREGATE_REMOVE
*   **Definition**: Eliminate a `GROUP BY` when it is redundant (e.g., the input is already distinct on the group keys, or no aggregate functions are used).
*   **Objective**: Remove unnecessary aggregation overhead.
*   **Conditions**: GROUP BY on columns that are already distinct; or SELECT uses only GROUP BY columns without aggregates.
*   **Mechanisms**: Remove GROUP BY; replace with SELECT DISTINCT if needed.

### 6.9. AGGREGATE_JOIN_REMOVE / AGGREGATE_JOIN_JOIN_REMOVE
*   **Definition**: Eliminate a LEFT/RIGHT JOIN feeding an aggregation when columns from the joined table are not referenced in the aggregate output.
*   **Objective**: Remove unnecessary joins that do not contribute to the final result.
*   **Conditions**: RIGHT table columns are unused in SELECT aggregates (or only used in DISTINCT aggregates); join keys are unique on the non-preserved side.
*   **Mechanisms**: Remove the join, adjust FROM and GROUP BY to reference only the preserved-side table.

### 6.10. AGGREGATE_JOIN_TRANSPOSE_EXTENDED
*   **Definition**: Push aggregate operations below a JOIN when aggregates reference columns from only one side.
*   **Objective**: Reduce data volume before the join by pre-aggregating.
*   **Conditions**: Equi-join only; join key columns are unique; aggregates are on columns from a single side; aggregates are merge-safe (SUM, COUNT, MIN, MAX).
*   **Mechanisms**: Push each side's applicable aggregates into subqueries, then join the pre-aggregated results.

### 6.11. AGGREGATE_UNION_TRANSPOSE
*   **Definition**: Push aggregate operations below a UNION ALL.
*   **Objective**: Perform aggregations on smaller partitioned data before combining results.
*   **Conditions**: UNION ALL followed by an aggregate; pushing down the aggregate preserves semantics.
*   **Mechanisms**: Apply aggregate to each UNION ALL branch; replace outer `COUNT()` with `SUM()` of per-branch counts; add a final aggregation layer.

### 6.12. AGGREGATE_UNION_AGGREGATE_FIRST / AGGREGATE_UNION_AGGREGATE_SECOND
*   **Definition**: Optimize patterns where UNION ALL combines GROUP BY results without aggregates (dedup-only GROUP BY) — merge the GROUP BY above or below the UNION ALL.
*   **Objective**: Consolidate redundant grouping operations across UNION ALL branches.
*   **Mechanisms**:
    *   **FIRST**: Remove GROUP BY from UNION ALL branches, apply a single GROUP BY after UNION ALL.
    *   **SECOND**: When only the right branch has GROUP BY (no aggregates), combine via UNION ALL then apply a single outer GROUP BY.

### 6.13. AGGREGATE_VALUES
*   **Definition**: Replace aggregate results with literal values when the input is known to be empty.
*   **Objective**: Avoid executing aggregations on provably empty inputs.
*   **Conditions**: Input dataset is known to be empty; aggregate functions like COUNT, SUM, MIN, MAX are used.
*   **Mechanisms**: Replace `COUNT/SUM` with `0`, replace `MIN/MAX` with `NULL`.

### 6.14. AGGREGATE_ANY_PULL_UP_CONSTANTS
*   **Definition**: Remove constant-valued columns from the GROUP BY clause and project them as literals.
*   **Objective**: Reduce the number of group keys, enabling more efficient aggregation.
*   **Conditions**: GROUP BY includes columns with constant values across all rows.
*   **Mechanisms**: Remove constant columns from GROUP BY, wrap in a subquery, and project the constants back in the outer SELECT.

## 7. JOIN_OPTIMIZATION_STRATEGIES

### 7.1. JOIN_CONDITION_PUSH
*   **Definition**: Move WHERE conditions that reference only one side of a join into the ON clause or into a subquery on that side.
*   **Objective**: Reduce the cardinality of join inputs before the join operation.
*   **Conditions**: INNER JOIN conditions referencing one table → push to ON clause. OUTER JOIN conditions on the preserved side → push to ON clause. OUTER JOIN conditions on the non-preserved side that eliminate NULLs effectively convert to INNER JOIN.
*   **Mechanisms**: Push single-table WHERE conditions into ON clause; for OUTER JOINs, evaluate whether the condition preserves outer semantics before pushing.

### 7.2. JOIN_ADD_REDUNDANT_SEMI_JOIN
*   **Definition**: Introduce a semi-join filter on the left input of an INNER JOIN to eliminate rows without matches in the right table before the full join executes.
*   **Objective**: Reduce the left input cardinality before the join.
*   **Conditions**: INNER JOIN with a valid join condition.
*   **Mechanisms**: Wrap the left input in a subquery with EXISTS (SELECT ... FROM right WHERE condition) to pre-filter non-matching rows.

### 7.3. JOIN_PROJECT_BOTH_TRANSPOSE_INCLUDE_OUTER / JOIN_PROJECT_LEFT_TRANSPOSE_INCLUDE_OUTER / JOIN_PROJECT_RIGHT_TRANSPOSE_INCLUDE_OUTER
*   **Definition**: Move projection (SELECT expressions) from inside join inputs to above the join.
*   **Objective**: Reduce the complexity of intermediate join results by projecting only necessary columns before the join, then applying full projections after.
*   **Conditions**: Inner or outer join with SELECT subqueries on one or both sides; no window functions in the projections.
*   **Mechanisms**: Identify columns needed for the join condition; project only those from each input; apply the full projection as a SELECT above the join result. For OUTER JOINs, handle nullability with COALESCE/casts.

### 7.4. JOIN_SUB_QUERY_TO_CORRELATE / JOIN_TO_CORRELATE
*   **Definition**: Convert subqueries in join conditions (scalar, EXISTS, IN, SOME/ANY, UNIQUE) into correlated join operations.
*   **Objective**: Eliminate subquery execution overhead by converting to set-based join operations.
*   **Conditions**: Scalar/EXISTS/IN/SOME/UNIQUE subqueries in join ON clause.
*   **Mechanisms**: Scalar → LEFT JOIN + aggregate; EXISTS → LEFT JOIN + IS NOT NULL filter; IN → JOIN + WHERE non-null; SOME/ANY → JOIN with appropriate comparator; UNIQUE → JOIN + DISTINCT.

### 7.5. JOIN_PUSH_EXPRESSIONS
*   **Definition**: Push expressions that reference only one side of a join into a projection on that side.
*   **Objective**: Simplify join conditions by pre-computing expressions in a subquery.
*   **Conditions**: JOIN condition contains an expression using columns exclusively from one table.
*   **Mechanisms**: Add the expression as a new column in a SELECT subquery on that table; replace the expression in the ON clause with a direct column comparison.

### 7.6. JOIN_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and remove redundant casts in join conditions.
*   **Objective**: Reduce computational overhead in join condition evaluation.
*   **Conditions**: Join conditions contain constant expressions or redundant CAST operations.
*   **Mechanisms**: Evaluate constant expressions to literals; remove identity casts.

### 7.7. JOIN_EXTRACT_FILTER
*   **Definition**: Convert an INNER JOIN condition into a Cartesian product followed by a WHERE filter.
*   **Objective**: Enable other filter-based rewrites and standardize join representations.
*   **Conditions**: INNER JOIN with non-trivial ON condition (not simply TRUE).
*   **Mechanisms**: Replace INNER JOIN ON condition with CROSS JOIN; move the original ON condition into a WHERE clause.

### 7.8. JOIN_PUSH_TRANSITIVE_PREDICATES
*   **Definition**: Infer and apply additional filter predicates on join inputs derived from join conditions and existing predicates.
*   **Objective**: Reduce join input cardinalities through transitive closure of predicates.
*   **Conditions**: A JOIN with conditions that imply additional filters on one or both inputs.
*   **Mechanisms**: Infer new predicates from join conditions; wrap inputs in SELECT statements with WHERE clauses applying the inferred predicates.

### 7.9. JOIN_LEFT_UNION_TRANSPOSE / JOIN_RIGHT_UNION_TRANSPOSE
*   **Definition**: Push a JOIN through a UNION ALL on either side, applying the JOIN to each UNION ALL branch individually.
*   **Objective**: Enable per-branch optimization and reduce intermediate result sizes.
*   **Conditions**: A JOIN whose left or right operand is a UNION ALL; the JOIN is not correlated.
*   **Mechanisms**: Push the JOIN into each branch of the UNION ALL, then recombine with UNION ALL.

### 7.10. JOIN_DERIVE_IS_NOT_NULL_FILTER_RULE
*   **Definition**: Add `IS NOT NULL` filters on join key columns for INNER JOIN inputs.
*   **Objective**: Eliminate NULL-bearing rows that cannot participate in equi-joins.
*   **Conditions**: INNER JOIN with equi-join conditions.
*   **Mechanisms**: For each join key column, add `WHERE column IS NOT NULL` to the respective input subquery.

## 8. FILTER_PREDICATE_STRATEGIES

### 8.1. FILTER_MERGE
*   **Definition**: Combine consecutive filter operations on the same data into a single WHERE clause.
*   **Objective**: Reduce plan complexity and enable single-pass filtering.
*   **Conditions**: Two nested or successive WHERE clauses applying to the same dataset.
*   **Mechanisms**: Merge filter conditions with a logical AND into a single WHERE clause.

### 8.2. FILTER_SET_OP_TRANSPOSE
*   **Definition**: Push a filter below a set operation (UNION, INTERSECT, EXCEPT), applying it to each branch.
*   **Objective**: Reduce the cardinality of each set operation input.
*   **Conditions**: WHERE clause applied after a UNION/INTERSECT/EXCEPT.
*   **Mechanisms**: Push the filter condition (with adjusted column references) into each branch of the set operation.

### 8.3. FILTER_INTO_JOIN
*   **Definition**: Push filter conditions into the ON clause of an INNER JOIN or into a subquery on the appropriate side.
*   **Objective**: Filter rows before the join rather than after.
*   **Conditions**: Filter on INNER JOIN result referencing columns from one side only.
*   **Mechanisms**: Move single-table conditions into ON clause (or a WHERE clause on the subquery). For OUTER JOINs, handle with care to preserve outer semantics.

### 8.4. FILTER_PROJECT_TRANSPOSE
*   **Definition**: Push a filter below a projection (SELECT expressions), rewriting conditions to reference source columns.
*   **Objective**: Filter rows before expression evaluation.
*   **Conditions**: WHERE clause follows SELECT expressions; SELECT does not contain window functions; filter is not correlated.
*   **Mechanisms**: Rewrite filter conditions to reference original columns; apply filter before the projection via a subquery.

### 8.5. FILTER_AGGREGATE_TRANSPOSE
*   **Definition**: Push filter conditions on GROUP BY columns below the aggregation operation.
*   **Objective**: Reduce rows flowing into the aggregation.
*   **Conditions**: WHERE or HAVING conditions reference only GROUP BY columns.
*   **Mechanisms**: Move conditions on GROUP BY columns into a WHERE clause on a subquery feeding the aggregation; keep conditions on aggregate results in HAVING.

### 8.6. FILTER_SUB_QUERY_TO_CORRELATE
*   **Definition**: Convert scalar, IN, EXISTS, and UNIQUE subqueries in the WHERE clause into join-based operations.
*   **Objective**: Replace correlated subquery execution with more efficient join plans.
*   **Conditions**: Scalar/IN/EXISTS/UNIQUE subqueries in WHERE clause.
*   **Mechanisms**: Scalar → LEFT JOIN + aggregate; IN → INNER JOIN with IS NOT NULL; EXISTS → INNER JOIN + DISTINCT; UNIQUE → special handling with DISTINCT aggregation.

### 8.7. FILTER_CORRELATE
*   **Definition**: Decompose and push filter conditions into or out of correlated subqueries.
*   **Objective**: Reduce data processed by correlated subqueries by applying filters as early as possible.
*   **Conditions**: Correlated subquery with WHERE clause filters.
*   **Mechanisms**: Classify filters by column origin (outer-only, inner-only, mixed). Push inner-only filters into the subquery; push outer-only filters closer to the outer query; leave mixed filters in place.

### 8.8. FILTER_VALUES_MERGE
*   **Definition**: Apply filter conditions directly to the tuples in a VALUES clause, eliminating non-matching tuples.
*   **Objective**: Statically compute filter results when the input is a fixed set of literal tuples.
*   **Conditions**: WHERE clause applied to a VALUES-derived table with literal tuples.
*   **Mechanisms**: Evaluate filter conditions against each VALUES tuple; remove tuples that do not satisfy the condition; rewrite as a pruned VALUES clause.

### 8.9. FILTER_TABLE_FUNCTION_TRANSPOSE
*   **Definition**: Push filter conditions into the input of a table function when the function has a 1:1 column mapping.
*   **Objective**: Reduce data processed by table functions.
*   **Conditions**: Table function with 1:1 input-to-output column mapping; filter on function output.
*   **Mechanisms**: Rewrite filter conditions to reference input columns; apply filter before the table function invocation.

### 8.10. FILTER_EXPAND_IS_NOT_DISTINCT_FROM
*   **Definition**: Expand `IS NOT DISTINCT FROM` into equivalent CASE expressions for engines that do not natively support it.
*   **Objective**: Enable query execution on engines lacking `IS NOT DISTINCT FROM` support.
*   **Conditions**: WHERE clause uses `IS NOT DISTINCT FROM`.
*   **Mechanisms**: Replace `a IS NOT DISTINCT FROM b` with `CASE WHEN a IS NULL AND b IS NULL THEN TRUE WHEN a IS NULL OR b IS NULL THEN FALSE ELSE a = b END`.

### 8.11. FILTER_REDUCE_EXPRESSIONS
*   **Definition**: Simplify filter conditions through constant folding and logical simplification.
*   **Objective**: Eliminate tautologies, contradictions, and redundant conditions.
*   **Conditions**: WHERE clause contains evaluable constant expressions, tautologies (1=1), or contradictions (1=0).
*   **Mechanisms**: Remove always-true conditions; replace always-false queries with empty result sets; apply logical simplification (e.g., `x IS NOT NULL AND x IS NOT NULL` → `x IS NOT NULL`).

## 9. PROJECTION_STRATEGIES

### 9.1. PROJECT_MERGE
*   **Definition**: Combine two consecutive SELECT clauses (projections) into one.
*   **Objective**: Eliminate redundant projection layers.
*   **Conditions**: Two successive SELECT clauses; merging does not exponentially increase complexity.
*   **Mechanisms**: For identity projections, remove the redundant layer entirely. For reordering projections, combine column references. For complex cases, integrate inner expressions into the outer SELECT.

### 9.2. PROJECT_REMOVE
*   **Definition**: Eliminate an outer projection that is identical to the output of its input relation.
*   **Objective**: Remove unnecessary SELECT layers.
*   **Conditions**: Outer SELECT merely re-selects the same columns as the inner subquery without transformations or renaming.
*   **Mechanisms**: Remove the outer query; use the inner subquery directly.

### 9.3. PROJECT_FILTER_TRANSPOSE
*   **Definition**: Push a filter (WHERE) below a projection (SELECT) when possible.
*   **Objective**: Filter rows before computing projected expressions.
*   **Conditions**: SELECT does not contain window functions; filter does not depend on projected expressions.
*   **Mechanisms**: Move the WHERE clause into a subquery that applies to the base table, then apply the projections on top.

### 9.4. PROJECT_SET_OP_TRANSPOSE
*   **Definition**: Push projections below UNION/INTERSECT/EXCEPT operations.
*   **Objective**: Reduce column width in each set operation branch.
*   **Conditions**: Projection above a set operation; no window functions in the projection.
*   **Mechanisms**: Push non-windowed projections into each branch of the set operation; reapply window functions above if needed.

### 9.5. PROJECT_JOIN_TRANSPOSE
*   **Definition**: Push projections below JOIN operations, projecting only necessary columns from each side.
*   **Objective**: Minimize columns carried through the join, reducing I/O and memory.
*   **Conditions**: No window functions or nullable-to-non-nullable CASTs in projections.
*   **Mechanisms**: Create subqueries that select only the columns needed for the join condition and final output; join the reduced subqueries.

### 9.6. PROJECT_JOIN_REMOVE / PROJECT_JOIN_JOIN_REMOVE
*   **Definition**: Remove a join when the SELECT clause does not reference columns from one side and the join key is unique on that side.
*   **Objective**: Eliminate unnecessary joins.
*   **Conditions**: LEFT/RIGHT JOIN whose removed side columns are unused in SELECT; join key is unique on the removed side.
*   **Mechanisms**: Remove the join; adjust FROM to use only the needed table; for nested join patterns, rewrite the join chain.

### 9.7. PROJECT_AGGREGATE_MERGE
*   **Definition**: Merge a projection into an aggregation by removing unused aggregates and simplifying COALESCE patterns.
*   **Objective**: Streamline aggregate output by eliminating unused computations.
*   **Conditions**: Projection above an aggregation with potential unused aggregate functions or COALESCE wrappers.
*   **Mechanisms**: Remove aggregate functions not referenced in the outer SELECT; replace `COALESCE(SUM(x), 0)` with `SUM0(x)`.

### 9.8. PROJECT_WINDOW_TRANSPOSE
*   **Definition**: Push projections below window functions, selecting only columns needed for the window computation.
*   **Objective**: Reduce the working dataset before window function evaluation.
*   **Conditions**: Window functions present; not all columns from the base table are needed.
*   **Mechanisms**: Create a subquery selecting only columns referenced in the SELECT list, PARTITION BY, and ORDER BY of window functions; apply window functions on the reduced dataset.

### 9.9. PROJECT_TO_LOGICAL_PROJECT_AND_WINDOW
*   **Definition**: Split a SELECT clause containing window functions into a base projection (non-window expressions) followed by a separate window computation.
*   **Objective**: Decouple non-windowed expressions from window function computation for independent optimization.
*   **Conditions**: SELECT clause mixes window functions with regular expressions and column references.
*   **Mechanisms**: Separate SELECT expressions into non-window (inner subquery) and window-function (outer query) layers.

### 9.10. PROJECT_SUB_QUERY_TO_CORRELATE
*   **Definition**: Convert scalar subqueries and collection subqueries in the SELECT list into join-based operations.
*   **Objective**: Replace row-by-row subquery evaluation with set-based joins.
*   **Conditions**: Scalar subquery (returns one value) or collection subquery (ARRAY/MAP/MULTISET) in SELECT list. Also applies to IN/EXISTS/SOME/ANY subqueries in WHERE.
*   **Mechanisms**: Scalar → LEFT JOIN + aggregate; collection → JOIN + COLLECT aggregate; IN/EXISTS → (LEFT) JOIN with appropriate conditions.

### 9.11. PROJECT_VALUES_MERGE / PROJECT_FILTER_VALUES_MERGE
*   **Definition**: Apply projection and filter operations directly to VALUES clause tuples, computing results statically.
*   **Objective**: Eliminate runtime computation for operations on literal tuple sets.
*   **Conditions**: SELECT (and optionally WHERE) applied over a VALUES clause with literal tuples.
*   **Mechanisms**: Evaluate each SELECT expression against each VALUES tuple; apply filter conditions; produce a new VALUES clause with the transformed/pruned tuple set.

### 9.12. PROJECT_CORRELATE_TRANSPOSE
*   **Definition**: Push projections into correlated subqueries, limiting columns processed in the correlation.
*   **Objective**: Reduce columns carried through correlated execution.
*   **Conditions**: Projection (SELECT) over a correlated subquery; no window functions.
*   **Mechanisms**: Identify columns and expressions in the SELECT clause originating from the inner and outer parts of the correlation; add sub-selects in each side that project only necessary columns.

### 9.13. PROJECT_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and remove redundant CASTs in SELECT list.
*   **Objective**: Reduce computation by pre-evaluating constant expressions.
*   **Conditions**: SELECT list contains constant arithmetic/function expressions or redundant CAST operations.
*   **Mechanisms**: Evaluate constant expressions to literals; remove identity casts if nullability is preserved.

## 10. SORT_ORDER_STRATEGIES

### 10.1. SORT_REMOVE
*   **Definition**: Remove ORDER BY clauses when the input is already sorted on the same keys.
*   **Objective**: Eliminate redundant sorting operations.
*   **Conditions**: ORDER BY keys match the input's existing ordering; no LIMIT or OFFSET clause (unless pagination must be preserved).
*   **Mechanisms**: Remove the ORDER BY clause entirely when ordering is guaranteed by the input.

### 10.2. SORT_REMOVE_CONSTANT_KEYS
*   **Definition**: Remove constant expressions from ORDER BY clauses that do not affect ordering.
*   **Objective**: Simplify sort specifications.
*   **Conditions**: ORDER BY contains constant expressions or literals.
*   **Mechanisms**: If all sort keys are constants and no LIMIT/OFFSET → remove ORDER BY entirely. If mixed, remove only the constant sort keys.

### 10.3. SORT_UNION_TRANSPOSE / SORT_UNION_TRANSPOSE_MATCH_NULL_FETCH
*   **Definition**: Push ORDER BY + LIMIT below a UNION ALL, sorting each branch individually.
*   **Objective**: Reduce the data volume entering the UNION ALL by pre-sorting and limiting each branch.
*   **Conditions**: UNION ALL followed by ORDER BY + LIMIT (with or without OFFSET).
*   **Mechanisms**: Apply ORDER BY + LIMIT to each branch of the UNION ALL; apply a final ORDER BY + LIMIT on the combined result to ensure correct global ordering.

### 10.4. SORT_JOIN_TRANSPOSE
*   **Definition**: Push ORDER BY below an outer join when the sort keys reference only the preserved side.
*   **Objective**: Sort data before the join to enable more efficient merge or streaming.
*   **Conditions**: LEFT/RIGHT OUTER JOIN; ORDER BY references only preserved-side columns; LIMIT/OFFSET preserved correctly.
*   **Mechanisms**: Move ORDER BY into a subquery on the preserved-side table before the join.

### 10.5. SORT_PROJECT_TRANSPOSE
*   **Definition**: Push ORDER BY below a projection when sort keys are monotonic transformations of source columns.
*   **Objective**: Sort earlier in the pipeline, directly on base data.
*   **Conditions**: ORDER BY columns are directly represented in SELECT or are monotonic transformations of source columns.
*   **Mechanisms**: Apply ORDER BY to a subquery on the base table; apply SELECT projection on top of the sorted data.

## 11. SET_OPERATION_STRATEGIES

### 11.1. UNION_REMOVE
*   **Definition**: Remove a UNION ALL that operates on a single input (no actual union of distinct datasets).
*   **Objective**: Eliminate redundant set operations.
*   **Conditions**: UNION ALL applied to a single dataset without combining different data.
*   **Mechanisms**: Replace the UNION ALL with the underlying dataset directly.

### 11.2. UNION_TO_DISTINCT
*   **Definition**: Replace UNION (implicit DISTINCT) with UNION ALL + an outer DISTINCT.
*   **Objective**: Enable further optimizations by separating union from deduplication.
*   **Conditions**: UNION (implicitly UNION DISTINCT) combining multiple queries.
*   **Mechanisms**: Replace UNION with UNION ALL; wrap in a subquery with SELECT DISTINCT.

### 11.3. UNION_PULL_UP_CONSTANTS
*   **Definition**: Factor out constant expressions common across all branches of a UNION into an outer projection.
*   **Objective**: Reduce redundancy in UNION branches.
*   **Conditions**: Multiple UNION branches project identical constant expressions.
*   **Mechanisms**: Remove constants from UNION branches; add a wrapping SELECT that projects the constants alongside the UNION result.

### 11.4. INTERSECT_TO_DISTINCT
*   **Definition**: Rewrite INTERSECT operations using GROUP BY and UNION ALL for engines without native INTERSECT support.
*   **Objective**: Enable INTERSECT semantics through standard aggregation.
*   **Conditions**: INTERSECT operation across two or more sets.
*   **Mechanisms**: GROUP BY each input with COUNT; UNION ALL the grouped results; GROUP BY again with SUM; filter rows where total COUNT equals number of input sets.

## 12. SEMI_JOIN_STRATEGIES

### 12.1. SEMI_JOIN_JOIN_TRANSPOSE
*   **Definition**: Push a semi-join filter through a preceding JOIN, applying the semi-join to only the relevant side.
*   **Objective**: Reduce join input cardinality by filtering with the semi-join condition first.
*   **Conditions**: Semi-join (EXISTS/IN) after a JOIN; semi-join condition references only left or right side columns of the JOIN.
*   **Mechanisms**: Wrap the referenced side in a subquery with the semi-join condition; then perform the original JOIN.

### 12.2. SEMI_JOIN_PROJECT_TRANSPOSE
*   **Definition**: Push a semi-join below a projection to apply it directly on the base table.
*   **Objective**: Filter rows before computing projection expressions.
*   **Conditions**: Semi-join follows a projection; semi-join condition can be applied to the base table.
*   **Mechanisms**: Move the semi-join to act on the base table directly, removing the intervening projection from the semi-join scope.

### 12.3. SEMI_JOIN_FILTER_TRANSPOSE
*   **Definition**: Swap the order of a semi-join and a filter, applying the filter first to reduce the input to the semi-join.
*   **Objective**: Reduce the cardinality of the outer relation before semi-join evaluation.
*   **Conditions**: Semi-join followed by a filter on the outer relation.
*   **Mechanisms**: Wrap the outer relation in a subquery that applies the filter first; then apply the semi-join on the filtered result.

## 13. WINDOW_FUNCTION_STRATEGIES

### 13.1. WINDOW_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and redundant CASTs within window function arguments, PARTITION BY, and ORDER BY clauses.
*   **Objective**: Reduce computational overhead in window function processing.
*   **Conditions**: Window functions with constant operands, redundant casts, or constant partition/order keys.
*   **Mechanisms**: Evaluate constant operands to literals; remove redundant casts; replace constant PARTITION BY/ORDER BY keys with literals; reconstruct the window specification with optimized groups and collations.

## 14. SUBQUERY_UNNESTING_AND_CORRELATION_STRATEGIES

### 14.1. PROJECT_SUB_QUERY_TO_CORRELATE
*   **Definition**: Convert subqueries in the SELECT list (scalar, collection, IN, EXISTS) into join-based plans.
*   **Objective**: Replace tuple-by-tuple subquery execution with set-oriented joins.
*   **Mechanisms**: Scalar subquery → LEFT JOIN + aggregate; ARRAY/MAP/MULTISET → JOIN + COLLECT; IN → JOIN + filter; EXISTS → semi-join.

### 14.2. FILTER_SUB_QUERY_TO_CORRELATE
*   **Definition**: Convert subqueries in the WHERE clause (scalar, IN, EXISTS, UNIQUE) into joins.
*   **Objective**: Enable join-based optimization for filter subqueries.
*   **Mechanisms**: Scalar → LEFT JOIN + aggregate; IN → INNER JOIN; EXISTS → INNER JOIN + DISTINCT; UNIQUE → JOIN + DISTINCT aggregation.

### 14.3. JOIN_SUB_QUERY_TO_CORRELATE / JOIN_TO_CORRELATE
*   **Definition**: Convert subqueries in JOIN conditions into correlated join forms.
*   **Objective**: Standardize join representations for deeper optimization.
*   **Mechanisms**: Scalar subquery → LEFT JOIN + aggregate; EXISTS → LEFT JOIN + IS NOT NULL; IN → JOIN; SOME/ANY → JOIN with condition; UNIQUE → JOIN + DISTINCT.

## 15. EXPRESSION_SIMPLIFICATION_STRATEGIES

### 15.1. PROJECT_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and remove redundant CASTs in projection (SELECT) lists.
*   **Objective**: Pre-evaluate constant computations at optimization time.
*   **Conditions**: Constant arithmetic, string operations, or nested functions in SELECT list; redundant CASTs.
*   **Mechanisms**: Evaluate constants to literals; remove identity casts preserving nullability.

### 15.2. FILTER_REDUCE_EXPRESSIONS
*   **Definition**: Simplify filter conditions through constant folding and logical normalization.
*   **Objective**: Eliminate computationally redundant or tautological/contradictory conditions.
*   **Conditions**: Constant expressions, tautologies, contradictions, or redundancies in WHERE clauses.
*   **Mechanisms**: Fold constants; remove always-true conditions; replace always-false queries; apply logical redundancy elimination.

### 15.3. JOIN_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and redundant CASTs in join conditions.
*   **Objective**: Reduce join condition evaluation cost.
*   **Conditions**: Constant expressions or redundant CASTs in JOIN ON clause.
*   **Mechanisms**: Fold constants to literals; remove identity casts.

### 15.4. WINDOW_REDUCE_EXPRESSIONS
*   **Definition**: Simplify constant expressions and redundant CASTs in window function specifications.
*   **Objective**: Minimize overhead in window function execution.
*   **Conditions**: Window function operands, PARTITION BY, or ORDER BY contain constants or redundant CASTs.
*   **Mechanisms**: Fold constants; remove redundant casts; streamline the OVER() specification.
