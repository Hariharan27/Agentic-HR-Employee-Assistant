# Agentic HR & Employee Assistant

Phased implementation of one authenticated conversational service for Leave/HR, Onboarding, and Parking. The source-of-truth requirements are in `PROJECT_SPEC.md` and `ARCHITECTURE.md`.

## Current status

Phase 0, Phase 1A deterministic Leave, Phase 1B reusable confirmation, and Phase 1C policy retrieval are complete. Agent orchestration and the frontend remain gated behind later phases.

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

Not implemented yet: chat endpoint, LangGraph orchestration, Onboarding, Parking, and React UI.

## Docker startup

```bash
cp .env.example .env
docker compose up --build
```

The backend applies migrations and idempotently seeds demo identities before starting at `http://localhost:8000`.

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
- Interactive documentation: `http://localhost:8000/docs`

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

## Policy ingestion

Place policy PDFs in `backend/policy_docs`, then index them into Qdrant:

```bash
docker compose exec -T backend python -m app.rag.ingestion
```

Ingestion is idempotent per document. Each run replaces that document's existing vectors while preserving `document`, `page`, `section`, and `category` source metadata. Native PDF text is preferred; image-only pages use 300-DPI OCR with preprocessing and confidence checks. Visually blank pages are skipped, while unreadable non-blank pages fail ingestion instead of silently adding poor text.
