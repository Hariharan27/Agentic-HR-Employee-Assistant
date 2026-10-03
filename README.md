# Agentic HR & Employee Assistant

Phased implementation of one authenticated conversational service for Leave/HR, Onboarding, and Parking. The source-of-truth requirements are in `PROJECT_SPEC.md` and `ARCHITECTURE.md`.

## Current status

Phase 0, Phase 1A deterministic Leave, Phase 1B reusable confirmation, Phase 1C policy retrieval, Phase 1D conversational Leave, Phase 1E leave approval lifecycle, and the assessment frontend are complete. Onboarding and Parking remain gated behind later phases.

Included now:

- FastAPI application and health endpoint
- PostgreSQL/SQLAlchemy foundation models and Alembic migration
- JWT authentication with trusted `user_id`, `employee_id`, and `role`
- Employee, manager, and HR demo identities
- Generic conversation-session and pending-action persistence foundation
- Structured JSON logging with request IDs
- Backend/PostgreSQL/Qdrant Docker Compose services
- Deterministic authentication and authorization tests
- Phase 1A leave balances, holidays, working-day calculation, eligibility, and leave requests
- Phase 1B reusable pending actions with ownership, expiry, cancellation, replay protection, and atomic execution
- Phase 1C policy PDF ingestion with direct extraction, 300-DPI OCR fallback, BGE embeddings, Qdrant retrieval, source metadata, and insufficient-evidence handling
- Phase 1D authenticated chat endpoint, LangGraph orchestration, cost-aware Bedrock Mantle routing, grounded policy answers, deterministic leave tools, conversation state, and confirmed leave execution
- Phase 1E reporting-manager authorization, manager/HR approval queues, approve/reject/cancel transitions, atomic balance consumption, replay protection, and request audit history
- Phase 1E conversational manager queue, approval/rejection, employee cancellation, and audit-history intents with confirmation before every mutation
- Responsive Ideator PeopleDesk React interface with role-aware login, grounded chat sources, confirmation controls, and live leave/approval data

Not implemented yet: Onboarding and Parking workflows.

## Docker startup

```bash
cp .env.example .env
docker compose up --build
```

The backend applies migrations and idempotently seeds demo identities before starting at `http://localhost:8000`.
The React assessment UI is available at `http://localhost:5173`.

## Demo credentials

| Role | Username | Password |
|---|---|---|
| Employee | `employee` | `employee123` |
| Manager | `manager` | `manager123` |
| HR | `hr` | `hr12345` |

These are intentionally non-sensitive local demonstration values.

## API

- `GET /api/v1/health`
- `POST /api/v1/auth/login`
- `GET /api/v1/auth/me` (Bearer token required)
- `POST /api/v1/chat` (Bearer token required)
- `GET /api/v1/leave/requests` (employee request history)
- `POST /api/v1/leave/requests/{id}/cancel` (cancel a pending own request)
- `GET /api/v1/leave/requests/{id}/history` (authorized audit history)
- `GET /api/v1/manager/leave-requests` (direct-report queue; HR sees all)
- `POST /api/v1/manager/leave-requests/{id}/approve`
- `POST /api/v1/manager/leave-requests/{id}/reject`
- Interactive documentation: `http://localhost:8000/docs`
- React chat interface: `http://localhost:5173`

Example chat request:

```json
{
  "message": "How many casual leave days do I have?",
  "session_id": "optional-client-session-id"
}
```

Reuse the returned `session_id` for follow-up messages and confirmation. A leave application is never executed from model output: the API stores a validated pending action and requires a separate `yes` message in the same session.

The same confirmation rule applies to conversational lifecycle actions. Managers can ask to show
pending approvals and propose approval or rejection by request ID; employees can propose cancelling
their own pending request. Approval, rejection, or cancellation executes only after `yes` in the same
authenticated session.

## Bedrock Mantle model cascade

Set `BEDROCK_API_KEY` in the untracked `.env` file. The configured cascade is:

- `openai.gpt-oss-20b` at low reasoning effort for structured routing and field extraction
- `openai.gpt-oss-120b` for grounded policy and general responses
- `openai.gpt-oss-120b` with medium reasoning effort only when router confidence is below `COMPLEX_ESCALATION_THRESHOLD`

Each request is capped by `LLM_MAX_CALLS_PER_REQUEST` (default: 3). Deterministic leave operations do not use the reasoning models after routing, and automated tests use fake providers so they incur no model cost.

## Local backend development

Python 3.12 or newer is required.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export DATABASE_URL=postgresql+psycopg://hr_app:hr_app@localhost:5432/hr_assistant
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload
```

Run tests with:

```bash
cd backend
pytest
```

Run the frontend locally with:

```bash
cd frontend
npm install
npm run dev
```

Create a production frontend bundle with `npm run build`. Set `VITE_API_BASE_URL` when the API is
not available at `http://localhost:8000`.

## Golden behavior evaluation

The versioned live-model dataset is in `backend/evals/golden_v1.jsonl`. It evaluates routing,
field extraction, deterministic leave behavior, policy grounding, confirmation safety, scope
boundaries, adversarial prompts, and API security without relying on exact response wording.

```bash
cd backend
.venv/bin/python evals/run_golden.py --dry-run
.venv/bin/python evals/run_golden.py --repeat 3 --fail-under 0.95
```

HTML and JSON reports are written under the ignored `backend/evals/reports/` directory. Confirmed
write cases are skipped unless `--include-mutating` is supplied and should only be run against a
disposable database.

## Policy ingestion

Place policy PDFs in `backend/policy_docs`, then index them into Qdrant:

```bash
docker compose exec -T backend python -m app.rag.ingestion
```

Ingestion is idempotent per document. Each run replaces that document's existing vectors while preserving `document`, `page`, `section`, and `category` source metadata. Native PDF text is preferred; image-only pages use 300-DPI OCR with preprocessing and confidence checks. Visually blank pages are skipped, while unreadable non-blank pages fail ingestion instead of silently adding poor text.
