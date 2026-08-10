# checker_eval — Checker Evaluation Dataset

Labeled toy projects for measuring how well the ADCo checker detects issues that an automated code rewriter could introduce. Each sample pairs an `original/` codebase, an `intent.json` describing the intended optimization, an `optimized/` codebase that mimics the rewriter's output (with `ADCO_OPTIMIZED` provenance tags), and a ground-truth label.

## Dataset at a glance

- **20 samples**: 5 categories × (2 positive + 2 negative)
- **10 positive** (`polarity: pos`) — the optimization is applied correctly; the checker should return **PASS**
- **10 negative** (`polarity: neg`) — the optimization contains exactly one planted flaw in the sample's category; the checker should return **FAIL** and flag that category
- **Categories**: `correctness`, `safety`, `regression`, `completeness`, `performance_regression`
- **Language**: Python 3 (stdlib only) + a couple of SQL files; each project is a small, distinct-domain app (2–4 files)

## Sample layout

```
<NN>_<category>_<pos|neg>_<domain>/
├── meta.json      # ground truth label
├── intent.json    # intended optimization (IntentExtractorOutput shape)
├── original/      # pre-rewrite code (no ADCO tags)
└── optimized/     # post-rewrite code; changed files carry the ADCO_OPTIMIZED tag
```

### meta.json fields

| field | description |
|---|---|
| `id` | `01`–`20` |
| `name` | short snake_case app name |
| `category` | one of the 5 categories |
| `polarity` | `pos` (correct optimization) or `neg` (planted flaw) |
| `domain` | the toy-project domain (all 20 distinct) |
| `expected_label` | `Correct` / `Incorrect` |
| `expected_status` | `PASS` (positives) / `FAIL` (negatives) |
| `expected_category` | the category the checker must flag (negatives) or `null` |
| `description` | what the rewrite does |
| `flaw` | the planted flaw for negatives, `""` for positives |

### intent.json fields

Mirrors `rewriter/sub_agents/intent_extractor/models.py` (`IntentExtractorOutput`): `connection`, `queries`, `transactions`, `n_plus_one`, `concurrency`, `orm`, `optimization_targets` (list of `{file, description}`), `notes`.

## Planted flaws (negatives)

| category | sample | planted flaw |
|---|---|---|
| correctness | `02` library | inverted max-price condition (`>=` instead of `<=`) |
| correctness | `04` food delivery | aggregation query missing `GROUP BY` |
| safety | `06` social feed | SQL injection (f-string interpolation of user input) |
| safety | `08` ride-sharing | command injection (`os.system` with user input) |
| regression | `10` job board | new required parameter added, two of three callers not updated |
| regression | `12` ticketing | error handling removed and ok/error contract broken (error dicts missing `ok` key, exception conversion stripped) |
| completeness | `14` gym | stub left (`raise NotImplementedError`) |
| completeness | `16` parking | unfinished refactor (dangling import / removed helper) |
| performance_regression | `18` pet adoption | N+1 queries (query per row in loop) |
| performance_regression | `20` expense tracker | caching layer removed |

## Running the evaluation

```bash
uv run python scripts/eval_checker.py                      # full run, exit 1 on mismatches
uv run python scripts/eval_checker.py --limit 4            # first 4 samples
uv run python scripts/eval_checker.py --model gemini-2.5-pro
uv run python scripts/eval_checker.py --delay 10 --no-strict
```

Output: per-sample table, summary metrics (positive pass rate, negative detection rate per category, confusion matrix), and `scripts/results/checker_eval_<timestamp>.{csv,json}`.

Scoring:
- positive → **MATCH** iff actual status is `PASS`; otherwise `false_positive(WARN|FAIL)`
- negative → **MATCH** iff actual status is `FAIL` and `expected_category` is among the reported issue categories; otherwise `missed` or `wrong category`

## Validation

`tests/test_dataset.py` checks the dataset structure: naming, meta/intent schema, ADCO tag placement, py-compilable sources, and balanced category/polarity counts. Run with `uv run pytest tests/test_dataset.py`.
