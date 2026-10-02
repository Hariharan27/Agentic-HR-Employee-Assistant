class ApplicationError(Exception):
    status_code = 400
    code = "application_error"


class AuthenticationError(ApplicationError):
    status_code = 401
    code = "authentication_failed"


class AuthorizationError(ApplicationError):
    status_code = 403
    code = "forbidden"


class NotFoundError(ApplicationError):
    status_code = 404
    code = "not_found"


class ConflictError(ApplicationError):
    status_code = 409
    code = "conflict"


class ValidationError(ApplicationError):
    code = "validation_error"


class InsufficientLeaveError(ApplicationError):
    code = "insufficient_leave"


class PendingActionExpiredError(ApplicationError):
    status_code = 410
    code = "pending_action_expired"


class MissingPolicyEvidenceError(ApplicationError):
    status_code = 404
    code = "missing_policy_evidence"
