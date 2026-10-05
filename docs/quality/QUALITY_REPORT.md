# Quality Gate Report

PeopleDesk is checked in two ways: deterministic automated tests (no model calls) and a live
golden evaluation that sends real conversations to the running API and the configured Bedrock
models.

## Latest full live run — 5 Oct 2026

| Measure | Result |
|---|---|
| Golden cases | 117 (one skipped by design), each run **3 times** |
| Turns executed | 438 |
| Turns passed | 434 |
| **Pass rate** | **99.1%** (gate: ≥ 95%) |
| **Consistency across the 3 runs** | **99.3%** (gate: ≥ 95%) |
| Average latency per turn | 2.8 s |

### By category

| Category | Passed | Rate |
|---|---|---|
| api safety | 15 / 15 | 100.0% |
| leave action | 144 / 144 | 100.0% |
| leave balance | 27 / 27 | 100.0% |
| leave rules | 40 / 42 | 95.2% |
| manager workflow | 21 / 21 | 100.0% |
| onboarding workflow | 30 / 30 | 100.0% |
| parking workflow | 32 / 33 | 97.0% |
| policy | 83 / 84 | 98.8% |
| safety | 24 / 24 | 100.0% |
| scope | 18 / 18 | 100.0% |

The gate also requires 100% for safety, API safety, onboarding and parking. Safety, API safety and
onboarding are 100%; parking was 97.0% because of one reply wording, fixed below.

### The four misses and what changed

| Case | What happened | Fix (covered by regression tests) |
|---|---|---|
| calculate-weekend-only | The model provider returned 503 once | Transient; the runner records it and continues |
| calculate-reversed-range | The model silently swapped a reversed date range | The reply is now checked against the user's own dates and reports "End date must be on or after start date" |
| policy-pl-conversion-even | Policy answer wrote numbers in words ("six days") | The policy prompt requires digits |
| parking-missing-date | Asked for the date in different wording | Fixed reply: "Please provide the parking date." |

Full report: [golden-report-2026-10-05.html](golden-report-2026-10-05.html) (open in a browser)
· raw results: [golden-report-2026-10-05.json](golden-report-2026-10-05.json).

## Automated tests

**308 tests pass** (`cd backend && pytest -q`), covering authentication and roles, date
resolution, leave plans and rules, the shared agent loop (repair, grounding, finaliser),
onboarding approval and account activation, parking (two vehicles, car/bike slots, waitlist,
admin attendance), pending-action confirmation, manager lifecycle, policy RAG, live streaming,
tracing and the demo seed. Agent tests use scripted model simulators, so they run without a model.

## What the golden set checks

Each case states the expected domain and intent, phrases that must (or must not) appear, the
HTTP status (for example 403 for a role that may not act) and whether a confirmation must be
pending. Categories: policy, leave balance, leave rules, leave actions, manager workflow,
onboarding, parking, safety (prompt injection, impersonation), scope and API safety.

## Re-running it

```bash
docker compose up -d --build
docker compose exec -T backend python -m app.seed --reset-demo
cd backend && ./evals/run_quality_gate.sh      # pytest, then the golden set × 3 with the gate
```

Reports are written to `backend/evals/reports/`. Details: [backend/evals/README.md](../../backend/evals/README.md).
Every live run can also be inspected step by step in Langfuse (see the README's Observability
section).
