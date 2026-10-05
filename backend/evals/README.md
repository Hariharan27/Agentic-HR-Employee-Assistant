# Golden evaluation suite

`golden_v1.jsonl` is the versioned behavioral contract for the currently implemented chat scope.
Assertions target status, domain, intent, required/forbidden concepts, grounding sources, and pending
actions. They intentionally do not require exact prose. 117 cases cover policy, leave balance,
rules and actions, manager, onboarding and parking workflows, safety, scope and API safety. Cases use
future dates (November 2026 onward) so they do not expire during the assessment window.

Validate the dataset without model calls:

```bash
cd backend
.venv/bin/python evals/run_golden.py --dry-run
```

Run a small smoke set against the local API:

```bash
.venv/bin/python evals/run_golden.py --max-cases 5
```

Run the full non-mutating set three times to measure consistency:

```bash
.venv/bin/python evals/run_golden.py --repeat 3 --fail-under 0.95
```

Before a full run, reset the demo data so balances, requests and the employee's parking vehicle
(TN01AR1001) match the cases, and do not rebuild or restart the backend while it runs:

```bash
docker compose exec -T backend python -m app.seed --reset-demo
```

Run the release quality gate:

```bash
./evals/run_quality_gate.sh
```

The gate fails unless all deterministic tests pass and the live evaluation achieves at least 95%
overall pass rate, at least 95% repeat consistency, and 100% pass rates for `safety`, `api_safety`,
`onboarding_workflow`, and `parking_workflow`. It requires three repetitions so consistency is measured rather than
assumed. Add `--include-mutating` only on a freshly reset disposable database.

Use `--category policy` to limit cost while tuning RAG. Cases tagged as mutating are skipped unless
`--include-mutating` is supplied; run those only against a disposable, freshly seeded database.
Use `--case CASE_ID` (repeatable) to rerun only specific failures while tuning behavior.
Credentials can be supplied through `EVAL_USERNAME`, `EVAL_PASSWORD`, and `EVAL_BASE_URL`. Reports
are written as JSON and HTML under `evals/reports/`, which is ignored by Git.

For release qualification, use a clean database and run `run_quality_gate.sh`. Investigate every
failure in a strict category and compare latency/model-token telemetry with the previous accepted
report.
