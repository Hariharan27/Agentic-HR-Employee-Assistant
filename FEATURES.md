# Ideator PeopleDesk — Features and Agentic Behaviour

This document lists what each role can do, explains how onboarding, its outbound and inbound
emails, and the email status extraction work, and points out where the system is agentic (the
model decides) and where it is deterministic (code decides). Each item is marked:

- **Built**: working end to end in this repository
- **Simulated**: the workflow runs, but an external system is replaced by a local stand-in
- **Not built**: a known gap, listed so nothing is overstated

---

## 1. Features by role

Every user signs in with a username and password. The role comes from the account (JWT), never
from the chat text, and role checks return **403 before any model call**.

### Employee

| Feature | Example prompt | Status |
|---|---|---|
| HR policy questions with document and page sources (32 PDFs) | "How many days of WFH are allowed?" | Built |
| Leave balance by type | "What is my leave balance?" | Built |
| Working-day calculation (weekly offs and Chennai / Bengaluru holidays excluded) | "How many working days from 12 to 16 Oct?" | Built |
| Natural-language dates: separate days vs ranges, "next Wednesday", "15th next month" | "Casual leave on Tuesday and Sunday" | Built |
| Apply for leave with a confirmation; split across two leave types when one balance is short | "Apply 3 days earned leave next week" | Built |
| "Same leave next week" (shifts the last plan) | "Same dates next week" | Built |
| List and cancel own leave requests | "Cancel request 42" | Built |
| Register up to two vehicles, list, update and remove them (refused while a booking uses it) | "Register my vehicle" | Built |
| Separate car and motorcycle slots; the slot board shows only slots that fit the employee's vehicles | "Which parking slots are free tomorrow?" | Built |
| Reserve one slot for one or more dates; the slot's type picks the vehicle | "Book B-22 Monday to Wednesday" | Built |
| Waitlist when all fitting slots are taken; cancel a booking | "Join the waitlist for Friday" | Built |
| Forced password change on first login | — | Built |

### Manager

| Feature | Status |
|---|---|
| Queue of direct reports' pending leave requests | Built |
| Approve or reject (with a reason) after confirmation; balances update atomically | Built |
| Audit history of a leave request | Built |
| Start onboarding in chat, by the inline form, or both (one shared draft) | Built |
| Track onboarding status by request ID, name or email | Built |

### HR

| Feature | Status |
|---|---|
| Start onboarding for any department; track onboarding status | Built |
| Leave operations allowed by the backend rules | Built |

### HR Administrator

| Feature | Status |
|---|---|
| Pending onboarding approval queue | Built |
| Approve (activates the employee account, creates the login, code and leave balances, shows a one-time password) | Built |
| Reject with a reason | Built |
| Cannot approve a request they created themselves | Built |

### Parking Administrator

| Feature | Status |
|---|---|
| Daily reservation queue (ID, employee, vehicle, slot, status) | Built |
| Check-in, late cancellation with a reason, no-show, completion, no-show override | Built |
| Three no-shows in 30 days suspend new bookings for 14 days | Built |

---

## 2. Onboarding end to end

```text
Manager / HR (chat or form)
   │  update_onboarding_draft → build_onboarding_plan → prepare_onboarding
   ▼
Pending confirmation ──"yes"──► OnboardingRequest (PENDING_APPROVAL) + 5 provisioning tasks
   ▼
HR Administrator: "Approve request 16" → confirmation → account activated, request ACTIVE
   │  after the commit: three outbound emails (Finance, IT, Facilities)
   ▼
Departments reply ──► POST /api/v1/inbound/email ──► tasks IN_PROGRESS / COMPLETED
   ▼
All five tasks COMPLETED ──► request COMPLETED   ("Show onboarding status for Nila")
```

The five provisioning tasks created with every request:

| Task | Owning department |
|---|---|
| Request corporate email | IT |
| Request laptop | IT |
| Request permanent access card | Facilities |
| Request temporary access card | Facilities |
| Set up payroll and salary account | Finance |

### 2.1 Collecting the candidate (agentic)

- **Built.** The Onboarding Agent (`app/agent/onboarding_agent.py`, tools in
  `app/agent/onboarding_tools.py`) has 10 tools: `update_onboarding_draft`,
  `list_reporting_managers`, `check_employee_exists`, `build_onboarding_plan`,
  `prepare_onboarding`, `get_onboarding_status`, `list_onboarding_approvals`,
  `get_onboarding_request`, `prepare_onboarding_approval`, `prepare_onboarding_rejection`.
- The model reads free text ("Onboard Nila Raman as a Software Engineer joining next Monday in
  Chennai") and decides which fields to fill. `update_onboarding_draft` **rejects any value the
  user did not actually write**, so the model cannot invent an email, date or manager. The
  joining date is resolved by the shared Python date resolver.
- Whatever is missing, the reply lists every missing field and shows the inline form. The form
  posts structured values straight into the same draft without any model call.
- When the draft is complete, the code finishes `build_onboarding_plan` and `prepare_onboarding`
  even if the model stops early, so the user always reaches Confirm and Cancel.

### 2.2 Approval and account activation

- **Built.** `prepare_onboarding_approval` creates a pending action. On "yes" the
  `ApproveOnboardingHandler` (`app/application/onboarding/handler.py`) revalidates the request and,
  in **one transaction**, creates the employee, the user (with `must_change_password`), the
  leave balances (6 casual, 6 sick, 12 earned, carry-forward 8) and the employee code.
- The one-time password appears only in that reply and is masked from conversation history and
  Langfuse traces.

### 2.3 Outbound email to departments

- **Simulated.** After the approval commits, `after_commit` sends three emails through
  `EmailService.send_safely`:

  | To | Subject |
  |---|---|
  | Finance | `[Onboarding #16] Payroll Setup Required - Nila Raman (<employee code>) - Joining 12 October 2026` |
  | IT | `[Onboarding #16] IT Provisioning Required - …` |
  | Facilities | `[Onboarding #16] Access Provisioning Required - …` |

  Each body carries the employee details and the requested actions.
- **How it is built:** `EmailService` depends on an `EmailGateway` port
  (`app/application/notifications/ports.py`). The configured adapter is
  `ConsoleEmailGateway` (`app/infrastructure/notifications/email.py`), which **prints the email to
  the backend log** (`docker compose logs backend`). No real mail is sent.
- Delivery runs after the commit and failures are logged, so an email problem can never undo an
  approval.
- The department addresses come from settings (`FINANCE_NOTIFICATION_EMAIL`,
  `IT_NOTIFICATION_EMAIL`, `FACILITIES_NOTIFICATION_EMAIL`).
- **Not built:** an SMTP or SES adapter. It is a single class implementing `EmailGateway.send`;
  nothing else changes.

### 2.4 Inbound email from departments

- **Simulated.** `POST /api/v1/inbound/email` (`app/api/routes/inbound_email.py`) takes
  `{sender, subject, body}`, the shape an email provider's inbound webhook (SendGrid Inbound
  Parse, Mailgun Routes, SES) would post. In the demo the reply is posted with `curl`.
- **Not built:** a real mailbox or provider connection that calls this endpoint.

Processing pipeline (deterministic checks first, the model only interprets the text):

| Step | Who | How |
|---|---|---|
| 1. Authenticate the caller | Code | `X-Inbound-Token` compared in constant time with `INBOUND_EMAIL_TOKEN`; 503 when not configured, 401 when wrong |
| 2. Find the onboarding request | Code | Regex `[Onboarding #N]` in the subject (the tag our outbound email put there) |
| 3. Identify the department | Code | The sender must be exactly the configured Finance, IT or Facilities address; anything else is refused |
| 4. Limit what may change | Code | Department allowlist: Finance → payroll; IT → corporate email and laptop; Facilities → access cards |
| 5. Read the reply | **Model** | Status extraction, see 2.5 |
| 6. Validate the model's output | Code | Pydantic schema plus the allowlist; any task outside the department's list rejects the whole update |
| 7. Apply | Code | `OnboardingService.apply_inbound_task_updates`: row lock, request must be ACTIVE, task statuses updated, request becomes COMPLETED when all five tasks are completed, one transaction |

### 2.5 Status extraction from the email body

- **Built.** `OnboardingReplyInterpreter` (`app/application/onboarding/reply_interpreter.py`)
  sends the department, its allowed task types and the email body to GPT OSS 120B in JSON mode.
  The rules it is given: return only `{"updates":[{"task_type","status"}]}`; COMPLETED only when
  the email clearly says done or ready; IN_PROGRESS when work started, is ordered or expected
  later; leave out any task without a clear update; never use a task outside the list.
- The parser tolerates code fences and surrounding text, then validates with Pydantic (status can
  only be `IN_PROGRESS` or `COMPLETED`).
- Example: IT replies "The laptop has been ordered and will arrive Friday. Nila's mailbox
  nila.raman@ideas2it.com is ready." The result is LAPTOP → IN_PROGRESS and CORPORATE_EMAIL →
  COMPLETED; the response returns the interpreted updates and the request's new progress
  (for example 1/5 completed).
- This is a **single structured model call** (an extraction step), not a tool loop: the model
  only classifies, and the code decides what is allowed and writes.

### 2.6 Status tracking

- **Built.** "Show onboarding status for Nila" (Manager, HR, HR Admin) runs
  `get_onboarding_status`. The reply shows every task with its status and the completed count,
  and the side panel shows the same request.

### 2.7 Known gaps in the email flow

| Gap | Effect | Status |
|---|---|---|
| Real outbound delivery | Emails appear in the backend log only | Not built (adapter swap) |
| Real inbound mailbox or provider webhook | Replies are posted with `curl` | Not built (configuration) |
| Audit record of each inbound email | Task changes are stored, the raw email is not | Not built |
| Status can move backwards | A later "in progress" email can reopen a completed task | Not built (one rule in `apply_inbound_task_updates`) |
| Notifying HR when onboarding completes | `HR_NOTIFICATION_EMAIL` is configured but unused | Not built |
| Unit tests for the interpreter | Endpoint authentication is tested; extraction is exercised in the demo and live runs | Not built |

---

## 3. Where the system is agentic

### 3.1 What the model decides

| Decision | Where | Why it is agentic |
|---|---|---|
| Which domain a message belongs to | Router, GPT OSS 20B (`orchestrator.py`) | Structured route from free text; guards then correct known phrasings |
| Which tools to call, in what order, and when to stop | Leave, Onboarding and Parking Agents on the shared loop (`runtime.py`) | Native tool calling; each tool result is fed back and the model plans the next step |
| How to recover from a failed tool call | Shared loop | The exact error is fed back for one repair turn |
| Which candidate details a message contains | Onboarding Agent | The model maps free text onto draft fields |
| Which provisioning tasks an email reports, and their status | Reply interpreter | Structured extraction from an unstructured email |
| How to phrase a grounded policy answer | Policy RAG, GPT OSS 120B | Composes the answer from retrieved chunks and cites them |

### 3.2 A real tool loop, step by step

```text
User: "Book parking day after tomorrow"
  router         → parking / reserve_parking
  model          → resolve_dates("day after tomorrow")     → 2026-10-07
  model          → list_parking_slots([2026-10-07])        → car slots for TN01AR1001, bike slots for TN84P2145
  reply          → the slot board; asks which slot
User: "B-21"
  model          → build_parking_plan(slot="B-21")        → eligible, vehicle TN01AR1001 (car slot → the car)
  code finishes  → prepare_parking(plan_id)               → pending confirmation
User: "yes"     → revalidated plan, fingerprint checked   → reservation written + audit event
```

Every step appears live in the UI (streamed over SSE) and stays in the "Agent activity" panel,
and in Langfuse as nested observations with model token usage.

### 3.3 Safeguards around the agent

| Guard | Effect |
|---|---|
| Identity injected from the JWT | The model never supplies who the user is |
| Tools filtered by role | A role only sees the tools it may use |
| Values must come from the user | Slot codes, vehicles and onboarding fields the user did not write are refused |
| Deterministic business rules | Dates, working days, balances, eligibility, slot types, splits |
| Grounding check | A number in the reply that no tool returned triggers a repair |
| Bounded loop | Limits on rounds, tool calls and repeated calls |
| Finaliser | Completes a plan → prepare step the model stopped short of |
| Pending action | Every write waits for an explicit "yes", is revalidated, then written atomically with an audit event |
| Fixed replies for safety-critical wording | Missing details, no vehicle, role refusals |

### 3.4 What is deliberately not agentic

Authorization, calendar arithmetic, leave and parking rules, inbound sender checks, correlation
by subject tag, transactions and audit events are plain code. The model chooses and phrases; it
never decides who someone is, what they may do, or commits a change.

---

## 4. Where to see it in the demo

| Feature | Demo step |
|---|---|
| Onboarding in natural language, approval, outbound emails | [DEMO_FLOW.md](DEMO_FLOW.md) — HR onboarding and HR Admin sections; emails in `docker compose logs backend` |
| Inbound department emails and status extraction | DEMO_FLOW.md — the `curl` inbound email steps, then "Show onboarding status" |
| Live agent steps | Any leave or parking request in the UI |
| Traces | Langfuse UI at `http://localhost:3000` |
