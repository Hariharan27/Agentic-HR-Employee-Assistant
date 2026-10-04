# Ideator PeopleDesk — End-to-End Demo Runbook

This is the repeatable assessment flow for creating an employee, approving the account, logging in
as that employee, using HR self-service, and demonstrating workplace parking.

Target duration: 10–12 minutes

## Roles used in the demo

| Demo responsibility | Person | Username | Password |
|---|---|---|---|
| Creates onboarding request and later approves leave | Saanvika Sree (Manager) | `manager` | `manager123` |
| Independently approves onboarding and activates account | Alaguselvi (HR Admin) | `hradmin` | `hradmin123` |
| Fallback employee account | Advik (Employee) | `employee` | `employee123` |
| Operates the parking arrival queue | Dhaswanth (Parking Admin) | `parkingadmin` | `parkingadmin123` |

The newly onboarded employee receives a generated username and one-time temporary password. Write
them down when HR Admin approves the request because the password is shown only once.

## Important role rule

The onboarding form displays **Account role: Employee**. This is the only role available in this
workflow. The backend also enforces `EMPLOYEE` when it activates the account, so a manager cannot
create a Manager, HR, HR Admin, or Parking Admin account through chat. **Designation** is the
employee's job title; it is not an authorization role.

## Before the demo

1. Make sure the Bedrock Mantle key is configured in `.env`. Do not display the file or key.
2. Start the application:

   ```bash
   docker compose up -d
   docker compose ps
   ```

3. Reset only the local demo data:

   ```bash
   docker compose exec -T backend python -m app.seed --reset-demo
   ```

4. Open [http://localhost:5173](http://localhost:5173).
5. Choose a parking date called `<PARKING_DATE>` that is at least two days in the future and no more
   than 30 days away. Use its exact `YYYY-MM-DD` value during the demo.
6. Do not run the reset command again until the complete demo is finished; reset removes accounts
   activated from demo onboarding requests.

## Part 1 — Manager creates the employee

1. Select **Manager** and sign in as `manager` / `manager123`.
2. Click **Start onboarding in chat**, or ask:

   ```text
   Start onboarding a new employee
   ```

3. Complete the inline form with this reusable sample:

   | Field | Demo value |
   |---|---|
   | Name | Nila Raman |
   | Email | `nila.demo@ideas2it.com` |
   | Account role | Employee |
   | Designation | Software Engineer |
   | Department | Engineering |
   | Reporting manager | Saanvika Sree |
   | Joining date | `2026-10-12` |
   | Employment type | Permanent |
   | Location | Chennai |

4. Click **Create request for confirmation**.
5. Point out that the response separates **Account role** from **Designation**, resolves the selected
   reporting manager, and previews these four provisioning tasks:

   - Corporate email
   - Laptop
   - Permanent access card
   - Temporary access card

6. Click **Confirm**. Record the returned onboarding request ID as `<ONBOARDING_ID>`.

What this proves: role-aware access, a structured form inside chat, manager lookup from PostgreSQL,
validation before mutation, and explicit human confirmation.

## Part 2 — HR Admin approves and activates the account

1. Sign out and select **HR Admin**.
2. Sign in as `hradmin` / `hradmin123`.
3. The approval queue should show Nila's request. You can also ask:

   ```text
   Show pending onboarding approvals
   ```

4. Ask using the real ID:

   ```text
   Approve onboarding request #<ONBOARDING_ID>
   ```

5. Click **Confirm**.
6. Record all three values shown in the response:

   ```text
   Employee code: <NEW_EMPLOYEE_CODE>
   Username: <NEW_USERNAME>
   Temporary password: <NEW_PASSWORD>
   ```

What this proves: maker-checker approval, HR Admin authorization, atomic employee/user creation,
default leave-balance creation, and one-time credential delivery.

## Part 3 — Log in as the newly created employee

1. Sign out.
2. On the login page, type `<NEW_USERNAME>` and `<NEW_PASSWORD>` manually. The generated username is
   normally the new employee code.
3. Sign in and point out that the profile shows Nila with the **EMPLOYEE** role.
4. Ask these questions one at a time:

   ```text
   What is my leave balance?
   ```

   Expected: 6 Casual, 6 Sick, and 12 Earned days available, with Earned Leave carry-forward capped
   at 8 days.

   ```text
   How many paid leave days do employees receive in a year?
   ```

   Expected: a grounded policy answer with the Revised Leave Policy source and page references.

   ```text
   Which leave type can be carried forward?
   ```

   Expected: Earned Leave only, up to 8 unused days.

   ```text
   List my leave requests
   ```

   Expected: no requests yet. This shows that data is isolated to the authenticated employee.

## Part 4 — New employee applies for leave

Use a future working date that does not overlap another request. For the prepared demo baseline:

```text
Apply casual leave on 2026-10-13 for a personal appointment
```

1. Check the calculated date, leave type, and working-day count.
2. Click **Confirm**.
3. Record the returned leave request ID as `<LEAVE_REQUEST_ID>`.
4. Ask:

   ```text
   Show my pending leave requests
   ```

Expected: the card explicitly labels `Request ID #<LEAVE_REQUEST_ID>`, making it usable for cancel
or manager approval commands.

Optional cancellation branch:

```text
Cancel leave request #<LEAVE_REQUEST_ID>
```

Do not cancel it if you want to show manager approval next.

## Part 5 — Reporting manager approves the new employee's leave

1. Sign out and sign in as Manager (`manager` / `manager123`).
2. Ask:

   ```text
   Show my pending leave approvals
   ```

3. Confirm that Nila's request appears because Saanvika was selected as the reporting manager during
   onboarding.
4. Ask:

   ```text
   Approve leave request #<LEAVE_REQUEST_ID>
   ```

5. Click **Confirm**.
6. Optionally ask:

   ```text
   Show history for leave request #<LEAVE_REQUEST_ID>
   ```

What this proves: the onboarding reporting relationship immediately drives manager authorization
for later employee workflows.

## Part 6 — New employee vehicle registration and parking

Vehicle details are not invented during onboarding; the authenticated employee registers them
explicitly after account activation.

1. Sign out from the Manager account used for leave approval.
2. Sign back in with Nila's generated `<NEW_USERNAME>` and `<NEW_PASSWORD>`.
3. Confirm that the profile shows Nila with the **EMPLOYEE** role.
4. Ask:

   ```text
   Register my vehicle
   ```

5. Complete the inline form:

   | Field | Demo value |
   |---|---|
   | Registration number | `TN01NL2026` |
   | Vehicle type | Car |
   | Make and model | Tata Nexon |

6. Click **Continue to confirmation**.
7. Confirm that PeopleDesk previews `TN01NL2026`, `Car`, and `Tata Nexon` without saving yet.
8. Click
   **Confirm**.

   Expected:

   ```text
   Vehicle TN01NL2026 was registered successfully as a car (Tata Nexon). You can now reserve parking.
   ```

9. Verify the stored vehicle:

   ```text
   What is my registered vehicle?
   ```

10. Check availability using the date chosen before the demo:

   ```text
   Is parking available on <PARKING_DATE>?
   ```

11. Reserve it:

   ```text
   Reserve parking on <PARKING_DATE>
   ```

12. Click **Confirm** and record `<PARKING_RESERVATION_ID>`.
13. Ask:

   ```text
   Show my parking reservations
   ```

14. If you want to demonstrate cancellation, ask before 8:00 PM on the day before the reservation:

   ```text
   Cancel my parking reservation on <PARKING_DATE>
   ```

   Click **Confirm**. Skip cancellation if you want to show the booking in the Parking Admin queue.

What this proves: authenticated vehicle registration, normalization and uniqueness validation,
live slot availability, confirmation-time revalidation, and employee-only reservation access.

## Part 7 — Parking Admin queue

1. Keep the parking booking active, sign out, and select **Parking Admin**.
2. Sign in as `parkingadmin` / `parkingadmin123`.
3. Open **Parking**, select `<PARKING_DATE>`, and refresh the queue; or ask:

   ```text
   Show parking admin queue for <PARKING_DATE>
   ```

4. Point out the reservation ID, employee, vehicle, slot, and status.

Check-in is deliberately time controlled. It opens at 7:00 AM on the reservation date. The arrival
cutoff is 11:00 AM, followed by a 15-minute grace period; after that, late check-in requires a reason
and a no-show can be recorded. Three no-shows within 30 days suspend new bookings for 14 days.

For a same-day demo during the valid window, use:

```text
Check in parking reservation #<PARKING_RESERVATION_ID>
```

Then click **Confirm**. Do not attempt this against a future reservation; the service will correctly
reject it.

## Suggested closing explanation

> PeopleDesk uses the LLM to understand natural language and extract structured intent. LangGraph
> routes the workflow, but deterministic services authorize the JWT identity, calculate leave,
> validate policy rules, recheck parking availability, and write transactions. Sensitive mutations
> require confirmation. Policy answers come from Qdrant with source citations, while employee,
> onboarding, leave, and parking data come from PostgreSQL tools.

## Quick fallback demo

If time is limited, show these five outcomes:

1. Manager creates Nila through the inline onboarding form.
2. HR Admin approves and returns one-time credentials.
3. Nila logs in and asks for leave balance plus one cited policy question.
4. Nila submits leave and Saanvika approves it.
5. Nila registers a vehicle, reserves parking, and Parking Admin shows it in the dated queue.

## Demo cautions

- Never display `.env`, JWTs, or the Bedrock API key.
- Always use the IDs returned by the current run; database sequences may not restart at `1`.
- Keep generated credentials only for the local demo and do not put them in source control.
- Use exact ISO dates for the most predictable recorded demo.
- Do not claim the LLM writes directly to PostgreSQL. Confirmed application services perform writes.
- Do not claim a vehicle is auto-created during onboarding; the employee registers it separately
  after first login.
- After the Manager leave-approval section, remember to sign back in as the generated employee before
  opening the vehicle-registration form.
