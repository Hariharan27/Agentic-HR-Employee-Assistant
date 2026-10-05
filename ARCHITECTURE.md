# Ideator PeopleDesk — As-Built Technical Architecture

This document describes the implementation currently present in the repository. The assessment
release implements authenticated HR policy, leave, employee-onboarding, employee-parking, and
Parking Administrator attendance lifecycles end to end.

## 1. Architecture goals

- Provide one authenticated conversational HR interface.
- Ground policy answers in company documents with visible sources.
- Keep identity, authorization, calculations, and writes outside the LLM.
- Use deterministic application services for business-critical operations.
- Require explicit human confirmation before every mutation.
- Preserve conversation context and an auditable request lifecycle.
- Control inference cost with tiered models, deterministic guards, and call budgets.
- Remain extensible without presenting incomplete workflows as finished features.

## 2. Implemented stack

| Area | Technology |
|---|---|
| Frontend | React, TypeScript, Vite, Nginx |
| API | Python 3.12, FastAPI, Pydantic |
| Agent orchestration | LangGraph |
| LLM provider | Amazon Bedrock Mantle, OpenAI-compatible chat completions |
| Operational storage | PostgreSQL, SQLAlchemy, Alembic |
| Policy retrieval | Qdrant, BAAI/bge-small-en-v1.5 |
| PDF extraction | Native PDF text with 300-DPI OCR fallback |
| Authentication | JWT bearer tokens |
| Testing | Pytest and a versioned live-model golden dataset |
| Packaging | Docker and Docker Compose |

## 3. System overview

```mermaid
flowchart TB
    UI[Ideator PeopleDesk\nReact UI] -->|JWT + HTTPS/JSON| API[FastAPI API]
    API --> AUTH[JWT authentication\nand role checks]
    AUTH --> GRAPH[LangGraph orchestrator]

    GRAPH --> ROUTER[Intent router\nGPT OSS 20B]
    GRAPH --> CONFIRM[Pending-action\nconfirmation]
    ROUTER --> POLICY[Policy node]
    ROUTER --> LEAVE[Leave Agent subgraph]
    ROUTER --> PARKING[Parking node]
    ROUTER --> GENERAL[Deterministic general response]
    ROUTER --> UNSUPPORTED[Future-domain response]

    POLICY --> RAG[Policy knowledge service]
    RAG --> EMBED[BGE embeddings]
    EMBED --> QDRANT[(Qdrant)]
    POLICY --> ANSWER[Grounded response\nGPT OSS 120B]

    LEAVE --> LEAVE_MODEL[Bounded tool-calling loop\nGPT OSS 120B]
    LEAVE_MODEL --> LEAVE_TOOLS[Typed Leave tools]
    LEAVE_TOOLS --> SERVICE[Deterministic LeaveService]
    PARKING --> PARKING_SERVICE[Deterministic ParkingService]
    CONFIRM --> PENDING[PendingActionCoordinator]
    PENDING --> SERVICE
    SERVICE --> REPO[SQLAlchemy repositories]
    PARKING_SERVICE --> REPO
    REPO --> POSTGRES[(PostgreSQL)]

    GRAPH --> CONVERSATION[Conversation repository]
    CONVERSATION --> POSTGRES
```

The low-cost model proposes the top-level domain. Leave then runs a bounded tool-calling subgraph
where the model selects typed operations and consumes structured results. It does not receive
authority to identify a different employee, bypass role checks, calculate final business values, or
commit a transaction.

## 4. Repository layers

```text
frontend/                         React presentation
backend/app/api/                  HTTP routes, schemas, dependencies
backend/app/agent/                LangGraph state and orchestration
backend/app/application/          Leave and pending-action use cases
backend/app/domain/               Entities, enums, and deterministic rules
backend/app/rag/                  Policy extraction, chunking, ingestion, retrieval
backend/app/infrastructure/       Database, repositories, embeddings, Qdrant, LLM adapter
backend/evals/                    Versioned golden behavior suite
```

Dependencies point inward: HTTP and LangGraph call application services; services depend on ports
and domain objects; infrastructure implements storage and provider adapters.

## 5. Authenticated request lifecycle

```mermaid
sequenceDiagram
    participant U as Employee/Manager
    participant W as React UI
    participant A as FastAPI
    participant G as LangGraph
    participant S as Application service
    participant D as PostgreSQL/Qdrant

    U->>W: Enter message
    W->>A: POST /api/v1/chat + bearer token
    A->>A: Validate JWT and construct AuthenticatedUser
    A->>G: Message, session ID, trusted actor
    G->>D: Load conversation and pending action
    alt Pending action exists
        G->>S: Confirm or cancel validated action
        S->>D: Atomic transaction
    else Normal request
        G->>G: Route and apply deterministic guards
        G->>G: Leave Agent selects typed tools and consumes results
        G->>S: Invoke LeaveService or policy retrieval
        S->>D: Read operational data or policy chunks
    end
    G->>D: Persist conversation state
    G-->>A: Message, intent, sources, activity, pending summary
    A-->>W: Typed JSON response
    W-->>U: Answer and confirmation controls
```

The JWT supplies `user_id`, `employee_id`, and `role`. User text and model output cannot overwrite
those values.

## 6. Implemented LangGraph

The application compiles one graph with these nodes:

```mermaid
flowchart LR
    START --> RESOLVE[resolve_pending_action]
    RESOLVE -->|pending action| CONFIRM[confirmation]
    RESOLVE -->|none| ROUTER[router]
    ROUTER --> POLICY[policy]
    ROUTER --> LEAVE[leave]
    ROUTER --> PARKING[parking]
    ROUTER --> GENERAL[general]
    ROUTER --> UNSUPPORTED[unsupported]
    CONFIRM --> END
    POLICY --> END
    LEAVE --> LEAVE_AGENT[Leave Agent]
    LEAVE_AGENT -->|tool calls| LEAVE_TOOLS[Leave tool executor]
    LEAVE_TOOLS -->|results| LEAVE_AGENT
    LEAVE_AGENT --> END
    PARKING --> END
    GENERAL --> END
    UNSUPPORTED --> END
```

### Shared state

The graph carries:

- Session and authenticated actor identifiers
- Current user message and recent message history
- Active domain
- Validated `RouteDecision`
- Pending action and user-facing summary
- Response and policy sources
- Safe factual Leave agent activity events
- Per-request LLM call count
- Validated parking date context for safe follow-up turns

### Routing

The router produces a validated Pydantic object containing domain, intent, confidence, leave type,
dates, reason, and request ID. Post-model guards enforce important invariants:

- Explicit self-service balance questions route to the authenticated employee's balance.
- Dates and leave types cannot be invented when the user did not provide them.
- Manager decisions require a request ID; rejection requires a reason.
- Policy-manipulation prompt attacks route directly to grounded retrieval.
- Low-confidence or invalid router output can escalate to the complex model tier.
- Each request has a strict maximum number of model calls.

## 7. Application tools and deterministic boundaries

The Leave Agent uses a structured tool-call protocol inside the LangGraph subgraph. Its typed tool
executor validates each call and invokes trusted Python code directly; the model never receives a
repository or employee identity argument.

| Capability | Implementation | Source of truth |
|---|---|---|
| Policy search | `PolicyKnowledgeService.search` | Qdrant policy chunks |
| Leave balance | `LeaveService.get_leave_balance` | PostgreSQL |
| Working days | `LeaveService.calculate_leave_days` | Python rules + holidays |
| Eligibility | `LeaveService.check_leave_eligibility` | Rules, balance, overlaps |
| Leave application | Pending handler + `LeaveService` | Confirmed transaction |
| Employee requests | `LeaveService.get_my_leave_requests` | Authenticated employee |
| Manager queue | `LeaveService.get_managed_leave_requests` | Reporting hierarchy |
| Approve/reject | Pending handler + `LeaveService` | Authorized transaction |
| Cancel request | Pending handler + `LeaveService` | Request ownership |
| Audit history | `LeaveService.get_leave_request_history` | Request events |
| Policy search from Leave | `PolicyKnowledgeService.search` | Qdrant policy chunks and source metadata |
| Parking vehicle | `ParkingService.get_vehicle` | Authenticated employee vehicle |
| Parking availability | `ParkingService.check_availability` | PostgreSQL slots/reservations |
| Reserve/cancel | Pending handlers + `ParkingService` | Confirmed atomic transaction |
| Parking waitlist | Pending handler + `ParkingService` | PostgreSQL waitlist |
| Parking Admin queue | `ParkingService.get_daily_reservations` | `PARKING_ADMIN` role |
| Parking attendance | Pending handlers + `ParkingService` | Check-in, no-show, completion, override |

This separation prevents the LLM from performing arithmetic, generating authoritative employee
IDs, changing statuses, or constructing SQL.

## 8. Leave lifecycle

### Leave plans (agent path)

The Leave Agent never computes dates or day counts. It passes the employee's date words to
`resolve_dates` (`app/domain/leave/dates.py`), which returns exact dates and whether they are
separate days ("Tuesday and Sunday") or one range ("Tuesday to Sunday"). `build_leave_plan`
(`LeaveService.build_leave_plan`) validates those dates and returns a `LeavePlan`: contiguous
segments, weekends and holidays not counted, balance before and after, past-date rules (sick
leave up to 7 days back, other types from today), overlaps, and a fingerprint. The newest plan is
the conversation's active plan, persisted on the session and valid for 30 minutes.

"Apply it" means `prepare_leave_application(plan_id)`: the executor rebuilds the plan, requires
the same fingerprint, and stores an `apply_leave_plan` pending action. On confirmation the
handler rebuilds the plan again inside the transaction and creates one request per segment, so
the days the employee was shown are exactly the days submitted.

### Read operations

- Balance responses combine total, used, and pending days.
- Eligibility excludes weekends and seeded holidays.
- Active overlapping requests prevent duplicate leave applications.
- Employees can read only their own requests.
- Managers see direct-report requests; HR can see the authorized broader queue.

### Mutating operations

```text
User request
  → domain route → Leave Agent tool selection
  → validate fields and authorization
  → calculate/revalidate business rules
  → store expiring PendingAction
  → show summary
  → explicit yes/cancel
  → revalidate inside transaction
  → execute once
  → append audit event
  → remove pending action
```

Pending actions are owned by both session and authenticated user, expire automatically, and are
consumed atomically. Approval consumes balance only after confirmation. Replay attempts cannot
execute the same action twice.

### Leave Agent loop safeguards

- Tool errors and repeated calls are returned to the model as results, so it can correct its
  arguments or explain the factual reason; a second identical repeat ends the turn.
- Invalid model output gets one repair turn that shows the model the validation error
  (`LEAVE_AGENT_MAX_REPAIRS`).
- Grounding check: every number in the final answer must appear in this turn's tool results,
  the active plan, the conversation or today's date; otherwise the model regenerates once.
- If repair fails, the reply is rendered only from tool results already returned (plan summary,
  balance, request list or the tool error); the user's words are never pattern-matched to guess.
- Native provider tool calling is used when `LEAVE_AGENT_NATIVE_TOOLS=true`; check support first
  with `python -m app.llm.probe`. Otherwise the JSON decision protocol is used.

### Onboarding Agent

Onboarding runs on the same shared loop (`app/agent/runtime.py`) as the Leave Agent, with its own
typed tools (`app/agent/onboarding_tools.py`):

- Manager/HR: `update_onboarding_draft` (every value must be stated in the user's message;
  employment type Permanent/Contract/Intern, location Chennai/Bengaluru, joining date today or
  later, manager matched to one eligible employee), `build_onboarding_plan`, `prepare_onboarding`,
  `list_reporting_managers`, `check_employee_exists`, `get_onboarding_status`.
- HR Admin: `list_onboarding_approvals`, `get_onboarding_request`, `prepare_onboarding_approval`,
  `prepare_onboarding_rejection`.
- Role gates run before the agent and tool authorization failures are raised, so unauthorized
  requests return HTTP 403. Credentials are generated only at confirmation and never reach a model.
- The inline form posts structured `onboarding_form` values that are written straight into the same
  draft (no text parsing, no model call); details given in chat are returned as `onboarding_draft`
  and pre-fill the form.
- Department reply emails (`POST /api/v1/inbound/email`) require the `X-Inbound-Token` shared
  secret (`INBOUND_EMAIL_TOKEN`).

### Parking Agent

Employee booking conversations (availability, reserve, cancel, waitlist) run on the shared loop
with `app/agent/parking_tools.py`: `resolve_dates`, `get_vehicle`, `list_parking_slots` (every
active slot with free/taken per date; regular slots first, accessible B-25 last),
`build_parking_plan` (one chosen slot for one or more dates; dates where it is taken return the
free alternatives, and the employee picks another slot or the waitlist), `prepare_parking`
(pending `reserve_parking_plan`, rebuilt and fingerprint-checked at confirmation) and
`prepare_parking_cancellation`. A slot code or "waitlist" is accepted only when the employee wrote
it, so the assistant never chooses a slot. Vehicle registration, the reservation list and Parking
Admin actions keep their deterministic handlers.

## 9. Policy RAG

### Ingestion

```text
32 policy PDFs
  → native text extraction
  → 300-DPI OCR fallback for image-only pages
  → extraction confidence checks
  → section-aware overlapping chunks
  → BGE embeddings
  → idempotent document replacement in Qdrant
```

Each chunk preserves document, page, section, and category metadata. Unreadable non-blank pages
fail ingestion instead of silently creating poor evidence.

### Retrieval and answer generation

```text
Policy question
  → query expansion for selected HR terminology
  → query embedding
  → top-k Qdrant search with score threshold
  → context containing source boundaries
  → constrained GPT OSS 120B answer
  → response cleanup
  → grouped source metadata in the UI
```

The answer prompt requires a direct, short, plain-text response and forbids adding unsupported
rules. No matches above the threshold produce an insufficient-evidence error rather than a
hallucinated answer.

## 10. Model routing and cost controls

| Tier | Model | Use |
|---|---|---|
| Router | `openai.gpt-oss-20b` | Normal structured routing with low reasoning effort |
| Standard | `openai.gpt-oss-120b` | Grounded policy response generation |
| Complex | `openai.gpt-oss-120b` | Low-confidence or invalid routing fallback with medium effort |

Cost and stability controls include:

- Deterministic business responses after routing
- Deterministic greeting/capability response
- Security and extraction guards before/after model routing
- Small router output budget
- Per-tier output-token limits
- Maximum model calls per request
- Retrieval limited to top-k evidence
- No reasoning-model call during confirmation execution

## 11. Persistence model

| Table | Purpose |
|---|---|
| `employees` | Employee profile and reporting manager relationship |
| `users` | Login identity, password hash, role, employee link |
| `conversation_sessions` | Recent conversation state and active domain |
| `pending_actions` | Expiring, single-use action awaiting confirmation |
| `leave_balances` | Total and used days by employee and leave type |
| `leave_requests` | Dates, working days, status, manager, decision metadata |
| `leave_request_events` | Immutable lifecycle/audit entries |
| `holidays` | Dates excluded from working-day calculations |
| `vehicles` | One registered active vehicle per employee for the parking MVP |
| `parking_slots` | Database-backed workplace parking inventory |
| `parking_reservations` | Reservation and attendance lifecycle state |
| `parking_reservation_events` | Auditable reservation status transitions and reasons |
| `parking_waitlist` | One active waitlist entry per employee and date |

Alembic owns schema evolution. PostgreSQL constraints, foreign keys, indexes, row locking, and
application transactions support integrity and concurrency safety.

## 12. Security controls

- Passwords use a modern one-way password hash.
- JWT expiry and signature validation happen in FastAPI dependencies.
- Employee identity is derived only from the validated token.
- Service-layer checks enforce employee ownership and manager/HR roles.
- Model-produced fields are validated by Pydantic and deterministic guards.
- Prompt injection cannot change policy evidence or bypass confirmation.
- CORS is restricted to configured origins.
- Secrets stay in the ignored `.env` file and are never returned to the UI.
- Structured logs include request IDs without logging credentials or JWTs.

## 13. Frontend

Ideator PeopleDesk provides:

- Employee, manager, and HR demo login selection
- JWT-authenticated session storage
- Role-aware prompts and request/approval panels
- Policy source display grouped by document and pages
- Pending-action Confirm and Cancel controls
- Responsive desktop/mobile layout
- Explicit status and error presentation

The frontend renders server decisions; it is not an authorization boundary.

## 14. Quality strategy

- **131 deterministic tests** cover authentication, security, leave rules, onboarding approval and
  account activation, parking persistence, employee workflows, allocation constraints, pending actions, manager
  lifecycle, RAG, orchestration, evaluation contracts, and repeatable demo seed.
- **96 versioned golden scenarios** exercise the live API and configured models across policy,
  leave, parking, safety, scope, manager workflow, and API safety categories.
- The latest complete live release gate scored **98.7%** with **99.0% consistency**, above both
  configured 95% thresholds; safety, API-safety, and onboarding categories passed at **100%**.
- Hardened regression scenarios for greeting stability, policy injection, and missing dates passed
  at **100%** after deterministic guards were added.
- The React production build is compiled with TypeScript before packaging.

## 15. Deployment and demo reset

Docker Compose runs four services:

```text
frontend :5173  → Nginx serving the Vite build
backend  :8000  → FastAPI/Uvicorn
postgres :5432  → operational database
qdrant   :6333  → policy vectors
```

Backend startup applies Alembic migrations and performs non-destructive idempotent seeding. Before
a recording, the explicit reset command clears only demo-identity activity and restores a known
balance and approval scenario:

```bash
docker compose exec -T backend python -m app.seed --reset-demo
```

## 16. Future extensions

Employee onboarding and parking now use the same validated, confirmed, atomic workflow pattern
as leave. Parking Phases 3A through 3C provide persistence, demo data, database allocation
constraints, employee availability, confirmed reservation and cancellation, own-booking lookup,
waitlisting, safe cross-domain conversation context, Parking Admin check-in, late cancellation,
no-show enforcement, three-strike suspension, and overrides. New domains can reuse the existing
pattern:

```text
validated route
  → domain application service
  → repository port
  → pending action for mutations
  → confirmation
  → atomic execution and audit event
```

This keeps the assessment release focused on one complete, demonstrable HR workflow while leaving
an intentional path for additional employee services.
