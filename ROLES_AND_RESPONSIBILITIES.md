# Ideator PeopleDesk — Roles and Responsibilities

This document describes the application roles, their responsibilities, and the authorization
boundaries implemented in Ideator PeopleDesk.

## Available roles

| Role | Primary responsibility | Demo user |
|---|---|---|
| Employee | HR self-service, leave requests, vehicle registration, and parking reservations | Advik |
| Manager | Direct-report leave decisions and new-employee onboarding initiation | Saanvika Sree |
| HR | Organisation-wide onboarding initiation and employee onboarding tracking | Hariharan |
| HR Administrator | Independent onboarding approval and employee-account activation | Alaguselvi |
| Parking Administrator | Parking attendance, exceptions, no-shows, and reservation lifecycle | Dhaswanth |

## Responsibility hierarchy

```text
Manager or HR
  └── creates an onboarding request
        ├── account role is fixed as Employee
        ├── reporting manager is selected
        └── request goes to HR Administrator
              └── approves and activates the employee account

Employee
  ├── asks policy questions
  ├── views balances and submits leave
  │     └── selected reporting manager approves or rejects it
  └── registers a vehicle and reserves parking
        └── Parking Administrator manages arrival and exceptions
```

The HR Administrator is an independent onboarding approver, not the employee's reporting manager.
The Parking Administrator is a separate operational role and does not approve leave or onboarding.

## Employee

### Responsibilities and permissions

- Sign in using an employee account.
- Ask questions grounded in company HR policy documents.
- View personal leave balances.
- Check personal leave eligibility and working-day calculations.
- Apply for Casual, Sick, or Earned Leave.
- View personal leave requests and their explicit request IDs.
- Cancel an eligible personal leave request.
- Register or update one active personal vehicle.
- View the registered vehicle.
- Check parking availability.
- Reserve and cancel personal parking bookings.
- Join the parking waitlist when all regular slots are occupied.
- View personal parking reservations and parking suspension status.

### Restrictions

- Cannot view another employee's private leave or parking data.
- Cannot approve or reject leave requests.
- Cannot create or approve onboarding requests.
- Cannot assign themselves a different application role.
- Cannot perform Parking Administrator actions.

All personal operations derive the employee ID from the authenticated JWT. An employee name or ID
written in chat cannot override that trusted identity.

## Manager

### Responsibilities and permissions

- Start onboarding for a new employee.
- Complete the structured onboarding form inside chat.
- Select the employee's reporting manager.
- Submit the onboarding request after explicit confirmation.
- Track onboarding requests the manager created or is authorized to manage.
- View pending leave requests belonging to direct reports.
- Approve or reject a direct report's leave request after confirmation.
- View the audit history of authorized leave requests.

### Restrictions

- Cannot approve their own onboarding request as HR Administrator.
- Cannot activate a new employee account.
- Cannot approve leave for employees outside their reporting scope.
- Cannot create Manager, HR, HR Administrator, or Parking Administrator accounts through onboarding.
- Cannot perform Parking Administrator actions.

## HR

### Responsibilities and permissions

- Start onboarding for employees across the organisation.
- Complete and submit the structured onboarding form.
- Select a reporting manager.
- Track onboarding requests.
- Perform the HR and manager leave operations allowed by backend authorization rules.

### Restrictions

- Does not independently activate an onboarding account.
- Cannot bypass the HR Administrator approval stage.
- Cannot create privileged application accounts through employee onboarding.
- Cannot perform Parking Administrator actions.

## HR Administrator

### Responsibilities and permissions

- View the pending onboarding approval queue.
- Review an onboarding request created by a Manager or HR user.
- Approve an onboarding request after explicit confirmation.
- Reject an onboarding request with a reason.
- Activate the employee profile and login atomically during approval.
- Generate the employee code, username, and one-time temporary password.
- Create the employee's initial leave balances:

  - 6 Casual Leave days
  - 6 Sick Leave days
  - 12 Earned Leave days
  - Earned Leave carry-forward limit of 8 days

### Restrictions

- Cannot approve an onboarding request they created themselves.
- Does not act as the employee's reporting manager unless separately represented by an authorized
  reporting relationship.
- Cannot use onboarding to create a privileged role.
- Does not manage parking attendance.

The temporary password is shown only once after approval and should be shared securely.

## Parking Administrator

### Responsibilities and permissions

- View parking reservations for a selected date.
- See reservation ID, employee, registered vehicle, allocated slot, and status.
- Check in an employee on the reservation date.
- Perform an administrator cancellation with a required reason.
- Mark a reservation as a no-show after the arrival cutoff and grace period.
- Correct an incorrectly recorded no-show with a reason.
- Mark a checked-in parking visit as completed.
- Maintain an auditable parking reservation history.

### Parking rules enforced by the service

- Check-in opens at 7:00 AM on the reservation date.
- Arrival cutoff is 11:00 AM.
- No-show grace period is 15 minutes.
- Employee cancellation cutoff is 8:00 PM on the day before the reservation.
- Three no-shows within 30 days suspend new parking reservations for 14 days.
- Late or exceptional administrator actions require an explanation where applicable.

### Restrictions

- Cannot create or approve employee onboarding requests.
- Cannot approve employee leave.
- Cannot silently change another employee's HR data.

## Onboarding role and reporting-manager rules

The current onboarding workflow is specifically for creating an **Employee** account.

| Onboarding field | Meaning |
|---|---|
| Account role | Authorization role; fixed and backend-enforced as `EMPLOYEE` |
| Designation | Job title, such as Software Engineer; does not grant system permissions |
| Department | Organisational department |
| Reporting manager | Employee responsible for approving the new employee's leave requests |

The reporting manager is mandatory. After HR Administrator approval, the selected manager's employee
ID is stored on the new employee record. Future leave requests use this relationship to determine the
correct approval queue.

Privileged identities should be provisioned separately by system administration. They must not be
created through the normal employee onboarding flow.

## Vehicle registration and parking ownership

Vehicle registration happens after the employee account is activated and the employee signs in.

```text
Employee asks "Register my vehicle"
  → completes the inline vehicle form
  → registration number is normalized and validated
  → duplicate ownership is rejected
  → employee confirms the action
  → one active vehicle is stored
  → parking reservation becomes available
```

The employee owns the accuracy of their vehicle details. The Parking Administrator manages parking
attendance and operational exceptions, not the employee's HR hierarchy.

## Approval ownership summary

| Action | Initiated by | Approved or managed by |
|---|---|---|
| Create employee onboarding request | Manager or HR | HR Administrator |
| Activate employee account | HR Administrator | Confirmation by the same authorized HR Administrator |
| Apply for leave | Employee | Selected reporting manager |
| Cancel employee leave | Employee | Employee confirmation and deterministic rules |
| Register or update vehicle | Employee | Employee confirmation and validation |
| Reserve or cancel parking | Employee | Employee confirmation and parking rules |
| Check-in, no-show, or parking exception | Parking Administrator | Parking Administrator confirmation |

## Demo credentials

| Role | Username | Password |
|---|---|---|
| Employee | `employee` | `employee123` |
| Manager | `manager` | `manager123` |
| HR | `hr` | `hr12345` |
| HR Administrator | `hradmin` | `hradmin123` |
| Parking Administrator | `parkingadmin` | `parkingadmin123` |

These credentials are for local demonstration only. Newly onboarded employees receive dynamically
generated credentials when their onboarding request is approved.
