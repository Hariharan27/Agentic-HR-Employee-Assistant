# Agentic HR & Employee Assistant — Technical Architecture

## 1. Architecture Goals

The architecture should:

- Support a single conversational interface.
- Support multiple enterprise workflows.
- Separate AI orchestration from business logic.
- Use deterministic services for business-critical operations.
- Support RAG for organizational knowledge.
- Support transactional operations safely.
- Maintain multi-turn conversation state.
- Require confirmation before side effects.
- Enforce authentication and authorization outside the LLM.
- Remain simple enough for an assessment/MVP.
- Be extensible for future enterprise workflows.

---

# 2. Technology Stack

Frontend:
- React
- TypeScript

Backend:
- Python 3.12+
- FastAPI
- Pydantic

Agent orchestration:
- LangGraph

LLM/tool abstraction:
- LangChain where useful

Operational database:
- PostgreSQL

ORM:
- SQLAlchemy

Vector database:
- Qdrant

Embeddings:
- BAAI/bge-small-en-v1.5

Authentication:
- JWT

Testing:
- Pytest

Deployment:
- Docker
- Docker Compose

Optional observability after MVP:
- Langfuse

---

# 3. High-Level Architecture

```text
┌───────────────────────────────────────────────────────────────┐
│                         React Chat UI                         │
│                                                               │
│        Login | Chat | Confirmation | Action Status            │
└───────────────────────────────┬───────────────────────────────┘
                                │
                              HTTPS
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                           FastAPI                             │
│                                                               │
│   Authentication                                              │
│   JWT validation                                              │
│   Request validation                                          │
│   POST /api/v1/chat                                           │
└───────────────────────────────┬───────────────────────────────┘
                                │
                                ▼
┌───────────────────────────────────────────────────────────────┐
│                    LangGraph Orchestrator                     │
│                                                               │
│                      Shared Agent State                       │
│                              │                                │
│                              ▼                                │
│                         Intent Router                         │
│                              │                                │
│             ┌────────────────┼────────────────┐               │
│             │                │                │               │
│             ▼                ▼                ▼               │
│       Leave Subgraph   Onboarding Subgraph Parking Subgraph   │
│             │                │                │               │
│             └────────────────┼────────────────┘               │
│                              │                                │
│                       Tool Invocation                         │
│                              │                                │
│                  Human Approval when needed                   │
└───────────────────────────────┬───────────────────────────────┘
                                │
                  ┌─────────────┼──────────────┐
                  │             │              │
                  ▼             ▼              ▼
          Application       RAG Service    Checkpoint/
           Services                         State Store
                  │             │
                  ▼             ▼
             PostgreSQL       Qdrant
```

---

# 4. Architectural Style

Use Clean Architecture principles.

```text
Presentation
     │
     ▼
Application / Agent
     │
     ▼
Domain
     │
     ▼
Infrastructure
```

## Presentation Layer

Contains:

- FastAPI routes
- Authentication dependencies
- Request/response schemas
- HTTP error mapping

It must not contain business rules.

---

## Agent/Application Layer

Contains:

- LangGraph
- Router
- Workflow nodes
- Tools
- Application services
- Use cases
- Pending-action coordination

Agent tools should be thin adapters into application services.

---

## Domain Layer

Contains:

- Domain entities
- Business rules
- Validation
- Domain exceptions
- Domain service interfaces

This layer should not depend on:

- FastAPI
- LangGraph
- Qdrant
- LLM providers

---

## Infrastructure Layer

Contains:

- SQLAlchemy repositories
- PostgreSQL implementation
- Qdrant adapter
- Embedding adapter
- LLM provider adapter
- External service adapters

---

# 5. Main Request Flow

```text
React
  │
  ▼
POST /api/v1/chat
  │
  ▼
JWT validation
  │
  ▼
AuthenticatedUser
  │
  ▼
Load conversation/checkpoint
  │
  ▼
LangGraph
  │
  ▼
Pending action?
  │
  ├── YES → confirmation handling
  │
  └── NO
       │
       ▼
     Router
       │
       ├── Leave
       ├── Onboarding
       ├── Parking
       ├── Policy
       └── General
       │
       ▼
Domain Subgraph
       │
       ▼
Tools / RAG
       │
       ▼
Final response
       │
       ▼
Persist state
       │
       ▼
React
```

---

# 6. LangGraph Architecture

Use one top-level graph.

Conceptually:

```text
START
  │
  ▼
load_context
  │
  ▼
resolve_pending_action
  │
  ├──── Pending confirmation ────► confirmation_node
  │
  ▼
router
  │
  ├────────────┬─────────────┐
  ▼            ▼             ▼
leave       onboarding     parking
subgraph     subgraph       subgraph
  │            │             │
  └────────────┴─────────────┘
               │
               ▼
        response_node
               │
               ▼
              END
```

A general/policy route can also be provided.

---

# 7. Shared Agent State

Conceptual state:

```python
class AgentState(TypedDict):
    messages: list
    session_id: str

    user_id: str
    employee_id: str
    role: str

    active_domain: str | None

    leave_context: dict
    onboarding_context: dict
    parking_context: dict

    pending_action: dict | None
```

Do not allow user messages or LLM output to overwrite trusted:

- user_id
- employee_id
- role

These values originate from authenticated server context.

---

# 8. Routing Architecture

Use structured LLM output.

Conceptual schema:

```python
class RouteDecision(BaseModel):
    domain: Literal[
        "leave",
        "onboarding",
        "parking",
        "policy",
        "general"
    ]
```

Example:

Input:

"Can you reserve parking for tomorrow?"

Output:

```json
{
  "domain": "parking"
}
```

Use LangGraph conditional edges to dispatch.

Do not use brittle parsing such as:

```python
if "parking" in llm_response:
```

Pending actions and active workflows should be resolved before invoking the general router.

---

# 9. Leave Subgraph

```text
                 leave_entry
                     │
                     ▼
              classify_leave_task
                     │
      ┌──────────────┼───────────────┐
      ▼              ▼               ▼
 policy_question   balance       eligibility
      │              │               │
      ▼              ▼        ┌──────┼──────┐
 policy_rag       balance     balance       holidays
                                │             │
                                └──────┬──────┘
                                       ▼
                                policy retrieval
                                       │
                                       ▼
                               eligibility service
                                       │
                                       ▼
                                application needed?
                                   /          \
                                 NO            YES
                                                │
                                         pending action
                                                │
                                          confirmation
                                                │
                                           apply_leave
```

Business calculations belong in LeaveService / LeaveEligibilityService.

---

# 10. Onboarding Subgraph

```text
              onboarding_entry
                     │
                     ▼
                extract_fields
                     │
                     ▼
             merge_with_context
                     │
                     ▼
            required_fields_complete?
                 /             \
               NO               YES
               │                 │
         ask_missing_fields      ▼
               │         authorization_check
               │                 │
               │                 ▼
               │        check_employee_exists
               │                 │
               │                 ▼
               │       determine_requirements
               │                 │
               │                 ▼
               │          build_action_plan
               │                 │
               │                 ▼
               │          pending confirmation
               │                 │
               │                 ▼
               │           execute_actions
               │                 │
               └─────────────────┤
                                 ▼
                           status_response
```

Use structured extraction for onboarding fields.

Do not ask the user again for information already available in state.

---

# 11. Parking Subgraph

```text
                parking_entry
                     │
                     ▼
                 extract_date
                     │
                     ▼
                  date known?
                 /          \
               NO            YES
               │              │
           ask_for_date       ▼
                        get_vehicle
                              │
                              ▼
                       check_availability
                          /          \
                    AVAILABLE        FULL
                        │              │
                        ▼              ▼
                    offer_slot    offer_waitlist
                        │              │
                        ▼              ▼
                 pending_action  pending_action
                        │              │
                        └───────┬──────┘
                                ▼
                          confirmation
                                │
                                ▼
                            execution
```

---

# 12. Tool Architecture

Tools are not business services.

Correct:

```text
LangGraph
   │
   ▼
Tool
   │
   ▼
Application Service
   │
   ▼
Repository Interface
   │
   ▼
Infrastructure Repository
   │
   ▼
PostgreSQL
```

Example:

```text
reserve_parking tool
        │
        ▼
ParkingService.reserve()
        │
        ▼
ParkingRepository.reserve()
        │
        ▼
PostgreSQL transaction
```

Tools should:

- Validate structured arguments
- Pass trusted user context
- Invoke application service
- Convert result into agent-friendly structured output

Tools should not contain large amounts of SQL or business logic.

---

# 13. Proposed Tool Registry

## Policy

- search_hr_policy

## Leave

- get_leave_balance
- get_holidays
- calculate_leave_days
- check_leave_eligibility
- apply_leave
- get_my_leave_requests

## Onboarding

- check_employee_exists
- determine_onboarding_requirements
- create_employee
- create_email_account_request
- create_equipment_request
- create_access_request
- assign_onboarding_tasks
- get_onboarding_status

## Parking

- get_employee_vehicle
- check_parking_availability
- reserve_parking
- get_my_parking_reservations
- cancel_parking
- join_parking_waitlist

---

# 14. RAG Architecture

## Indexing

```text
Policy PDFs
    │
    ▼
Document Loader
    │
    ▼
Text Normalization
    │
    ▼
Recursive Chunker
    │
    ▼
BGE Embeddings
    │
    ▼
Qdrant
```

Store metadata:

```json
{
  "document": "Leave Policy",
  "page": 4,
  "section": "Casual Leave",
  "category": "leave"
}
```

---

## Retrieval

```text
Question
   │
   ▼
Query Embedding
   │
   ▼
Qdrant
   │
   ▼
Top-K Chunks
   │
   ▼
Context Builder
   │
   ▼
LLM
   │
   ▼
Grounded Response
   +
Source Metadata
```

RAG answers should explicitly handle insufficient evidence rather than hallucinating policy.

---

# 15. Persistence Architecture

## PostgreSQL

System of record for transactional information.

Suggested tables:

```text
users
employees

leave_balances
leave_requests
holidays

onboarding_requests
onboarding_tasks

vehicles
parking_slots
parking_reservations
parking_waitlist
```

Use proper foreign keys and indexes.

---

# 16. Parking Concurrency

Parking reservations require transaction safety.

The following race must not occur:

```text
Employee A → sees B-24 available
Employee B → sees B-24 available

Employee A → reserves B-24
Employee B → reserves B-24
```

Protect with database constraints/transaction semantics.

At minimum, create a unique constraint logically equivalent to:

```text
(slot_id, reservation_date)
```

for active reservations.

Availability must be revalidated during reservation execution.

---

# 17. Authentication

```text
Login
  │
  ▼
Authentication Service
  │
  ▼
JWT
  │
  ▼
React stores token appropriately
  │
  ▼
Authorization: Bearer <token>
  │
  ▼
FastAPI JWT Dependency
  │
  ▼
AuthenticatedUser
  │
  ├── user_id
  ├── employee_id
  └── role
```

Trusted context is injected into agent execution.

---

# 18. Authorization

Authorization should be enforced in application services.

Example:

```text
create onboarding
      │
      ▼
OnboardingService
      │
      ▼
role in allowed_roles?
   /            \
 YES             NO
  │               │
continue       Forbidden
```

Never rely on:

"You are an employee, don't call this tool."

as the sole authorization mechanism.

---

# 19. Human-in-the-Loop Architecture

Use a generic PendingAction structure.

Conceptual model:

```text
PendingAction

action_type
tool_name
arguments
created_by
session_id
expires_at
```

Flow:

```text
Agent proposes mutation
       │
       ▼
Validate preliminary request
       │
       ▼
Store PendingAction
       │
       ▼
"Would you like me to proceed?"
       │
       ▼
User confirms
       │
       ▼
Reload PendingAction
       │
       ▼
Verify user/session
       │
       ▼
Revalidate business rules
       │
       ▼
Execute
       │
       ▼
Clear PendingAction
```

Confirmation should be generic enough to work for:

- leave
- onboarding
- parking

---

# 20. LLM Provider Abstraction

Do not tightly couple application code to one model vendor.

Provide an infrastructure abstraction/factory.

Example configuration:

```text
LLM_PROVIDER=
LLM_MODEL=
LLM_API_KEY=
```

The rest of the application should depend on an application-level LLM interface or centralized model factory.

This allows switching providers without rewriting workflows.

---

# 21. Suggested Backend Structure

```text
src/
├── main.py
│
├── api/
│   ├── routes/
│   │   ├── auth.py
│   │   └── chat.py
│   ├── schemas/
│   └── dependencies/
│       └── auth.py
│
├── agent/
│   ├── graph.py
│   ├── state.py
│   ├── router.py
│   ├── confirmation.py
│   │
│   ├── leave/
│   │   ├── graph.py
│   │   ├── nodes.py
│   │   ├── schemas.py
│   │   └── tools.py
│   │
│   ├── onboarding/
│   │   ├── graph.py
│   │   ├── nodes.py
│   │   ├── schemas.py
│   │   └── tools.py
│   │
│   └── parking/
│       ├── graph.py
│       ├── nodes.py
│       ├── schemas.py
│       └── tools.py
│
├── application/
│   ├── leave/
│   │   └── service.py
│   ├── onboarding/
│   │   └── service.py
│   ├── parking/
│   │   └── service.py
│   └── policy/
│       └── service.py
│
├── domain/
│   ├── employee/
│   ├── leave/
│   ├── onboarding/
│   └── parking/
│
├── infrastructure/
│   ├── database/
│   │   ├── models/
│   │   ├── repositories/
│   │   └── session.py
│   │
│   ├── vector_store/
│   │   └── qdrant.py
│   │
│   ├── embeddings/
│   └── llm/
│
├── rag/
│   ├── ingestion.py
│   ├── chunking.py
│   ├── retrieval.py
│   └── context_builder.py
│
└── core/
    ├── config.py
    ├── security.py
    ├── logging.py
    └── exceptions.py

tests/
├── unit/
├── integration/
└── agent/
```

The exact directory structure may evolve if a simpler structure improves maintainability, but architectural boundaries must remain.

---

# 22. Frontend Architecture

Keep frontend deliberately simple.

```text
React Application
      │
      ├── Login
      │
      └── Chat
            │
            ├── Message list
            ├── Input
            ├── Confirmation UI
            └── Observable action status
```

Do not create separate Leave, Onboarding and Parking screens for MVP.

The primary experience is conversational.

---

# 23. API Architecture

Primary APIs:

```text
POST /api/v1/auth/login
POST /api/v1/chat
GET  /api/v1/health
```

Domain APIs may exist internally for testing/administration but the frontend should primarily interact through chat.

Example chat request:

```json
{
  "session_id": "session-123",
  "message": "Book parking tomorrow"
}
```

Authenticated identity comes from JWT, not request body.

Example response:

```json
{
  "session_id": "session-123",
  "message": "Slot B-24 is available tomorrow. Would you like me to reserve it?",
  "requires_confirmation": true,
  "metadata": {
    "domain": "parking"
  }
}
```

Do not expose hidden model reasoning.

---

# 24. Observability

Use structured logging.

Useful fields:

```text
request_id
session_id
user_id
domain
node
tool
duration_ms
status
error_type
```

Optional later:

Langfuse tracing for:

- LLM calls
- retrieval
- agent transitions
- tool execution

Do not delay MVP completion for observability integration.

---

# 25. Docker Architecture

```text
                    Docker Compose

┌────────────────────────────────────────────┐
│                                            │
│ React Frontend                             │
│       │                                    │
│       ▼                                    │
│ FastAPI Backend                            │
│       │                                    │
│       ├────────────► PostgreSQL             │
│       │                                    │
│       ├────────────► Qdrant                 │
│       │                                    │
│       └────────────► External LLM API       │
│                                            │
└────────────────────────────────────────────┘
```

Required:

```text
docker-compose.yml
.env.example
```

The complete application should be startable with minimal setup.

---

# 26. Security Principles

1. Never trust LLM-generated identity.
2. Never expose arbitrary SQL to the LLM.
3. Never let the LLM directly mutate the database.
4. All writes pass through application services.
5. Authorization occurs in deterministic code.
6. Mutations require confirmation.
7. Revalidate before mutation.
8. Validate all tool arguments with Pydantic.
9. Keep secrets in environment variables.
10. Do not log tokens/secrets.
11. Scope employee data to authenticated identity.
12. Protect parking reservations with database constraints.

---

# 27. Architectural Principle

The architecture should maintain this separation:

```text
LLM
│
├── Understand
├── Extract
├── Route
├── Select tools
└── Generate natural language

Application Services
│
├── Validate
├── Authorize
├── Calculate
├── Execute business rules
└── Coordinate transactions

PostgreSQL
│
└── Transactional source of truth

Qdrant
│
└── Organizational knowledge retrieval
```

The model is an intelligent orchestration layer, not the source of truth.

---

# 28. Architecture Summary

The application uses a single LangGraph orchestrator behind one FastAPI chat endpoint.

The top-level graph routes natural-language requests to Leave, Onboarding or Parking subgraphs.

LangGraph manages conversational workflow and state.

Domain/application services contain deterministic business rules.

PostgreSQL stores transactional enterprise information.

Qdrant stores searchable policy knowledge.

JWT provides authenticated identity and role context.

All side-effecting actions require human confirmation and deterministic backend validation.

This architecture provides enough agentic behavior for the assessment while maintaining production-oriented separation of concerns and remaining achievable within the project deadline.