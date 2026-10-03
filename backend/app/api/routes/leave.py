from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, Database
from app.api.schemas.leave import (
    ApprovalRequest,
    CancellationRequest,
    LeaveRequestEventResponse,
    LeaveRequestResponse,
    RejectionRequest,
)
from app.application.leave.service import LeaveService
from app.domain.leave.entities import LeaveRequestData, LeaveRequestEventData
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository

router = APIRouter(prefix="/api/v1", tags=["leave lifecycle"])


def _service(db: Database) -> LeaveService:
    return LeaveService(SQLAlchemyLeaveRepository(db))


def _request_response(item: LeaveRequestData) -> LeaveRequestResponse:
    return LeaveRequestResponse(
        id=item.id,
        employee_id=item.employee_id,
        employee_code=item.employee_code,
        employee_name=item.employee_name,
        manager_employee_id=item.manager_employee_id,
        leave_type=item.leave_type.value,
        start_date=item.start_date,
        end_date=item.end_date,
        working_days=item.working_days,
        reason=item.reason,
        status=item.status.value,
        decided_by_user_id=item.decided_by_user_id,
        decision_comment=item.decision_comment,
        decided_at=item.decided_at,
    )


def _event_response(item: LeaveRequestEventData) -> LeaveRequestEventResponse:
    return LeaveRequestEventResponse(
        id=item.id,
        leave_request_id=item.leave_request_id,
        actor_user_id=item.actor_user_id,
        from_status=item.from_status.value if item.from_status else None,
        to_status=item.to_status.value,
        comment=item.comment,
        created_at=item.created_at,
    )


@router.get("/leave/requests", response_model=list[LeaveRequestResponse])
def my_leave_requests(actor: CurrentUser, db: Database) -> list[LeaveRequestResponse]:
    return [_request_response(item) for item in _service(db).get_my_leave_requests(actor)]


@router.post("/leave/requests/{request_id}/cancel", response_model=LeaveRequestResponse)
def cancel_leave_request(
    request_id: int, body: CancellationRequest, actor: CurrentUser, db: Database
) -> LeaveRequestResponse:
    return _request_response(_service(db).cancel_leave_request(actor, request_id, body.reason))


@router.get(
    "/leave/requests/{request_id}/history",
    response_model=list[LeaveRequestEventResponse],
)
def leave_request_history(
    request_id: int, actor: CurrentUser, db: Database
) -> list[LeaveRequestEventResponse]:
    events = _service(db).get_leave_request_history(actor, request_id)
    return [_event_response(item) for item in events]


@router.get("/manager/leave-requests", response_model=list[LeaveRequestResponse])
def managed_leave_requests(
    actor: CurrentUser,
    db: Database,
    status: str | None = Query(default="PENDING", min_length=1, max_length=24),
) -> list[LeaveRequestResponse]:
    return [
        _request_response(item)
        for item in _service(db).get_managed_leave_requests(actor, status)
    ]


@router.post(
    "/manager/leave-requests/{request_id}/approve",
    response_model=LeaveRequestResponse,
)
def approve_leave_request(
    request_id: int, body: ApprovalRequest, actor: CurrentUser, db: Database
) -> LeaveRequestResponse:
    return _request_response(
        _service(db).approve_leave_request(actor, request_id, body.comment)
    )


@router.post(
    "/manager/leave-requests/{request_id}/reject",
    response_model=LeaveRequestResponse,
)
def reject_leave_request(
    request_id: int, body: RejectionRequest, actor: CurrentUser, db: Database
) -> LeaveRequestResponse:
    return _request_response(
        _service(db).reject_leave_request(actor, request_id, body.reason)
    )
