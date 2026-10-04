from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.pending.ports import PendingActionHandler, PendingActionRepository
from app.core.exceptions import (
    AuthorizationError, ConflictError, NotFoundError, PendingActionExpiredError, ValidationError,
)
from app.core.security import AuthenticatedUser
from app.domain.pending.entities import PendingActionData


class PendingActionCoordinator:
    def __init__(
        self,
        repository: PendingActionRepository,
        handlers: dict[str, PendingActionHandler],
        *,
        ttl_minutes: int = 15,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.handlers = handlers
        self.ttl_minutes = ttl_minutes
        self.now = now or (lambda: datetime.now(UTC))

    def propose(self, actor: AuthenticatedUser, session_id: str, action_type: str,
                arguments: dict[str, Any], summary: str) -> PendingActionData:
        self._verify_session_owner(actor, session_id)
        if self.repository.get_pending(session_id) is not None:
            raise ConflictError("This conversation already has an action waiting for confirmation")
        handler = self.handlers.get(action_type)
        if handler is None:
            raise ValidationError(f"Unsupported pending action type: {action_type}")
        normalized_summary = " ".join(summary.split())
        if not normalized_summary or len(normalized_summary) > 500:
            raise ValidationError("Pending action summary must contain between 1 and 500 characters")
        validated_arguments = handler.validate_arguments(arguments)
        action = PendingActionData(
            id=None,
            session_id=session_id,
            user_id=actor.user_id,
            action_type=action_type,
            arguments=validated_arguments,
            summary=normalized_summary,
            expires_at=self.now() + timedelta(minutes=self.ttl_minutes),
        )
        try:
            created = self.repository.add_pending(action)
            self.repository.commit()
            return created
        except Exception:
            self.repository.rollback()
            raise

    def confirm(self, actor: AuthenticatedUser, session_id: str) -> Any:
        self._verify_session_owner(actor, session_id)
        action = self.repository.get_pending(session_id, for_update=True)
        if action is None:
            raise NotFoundError("No action is waiting for confirmation in this conversation")
        if action.user_id != actor.user_id:
            raise AuthorizationError("This pending action belongs to another user")
        if self._is_expired(action.expires_at):
            self.repository.delete_pending(action.id)
            self.repository.commit()
            raise PendingActionExpiredError("The pending action expired; please request it again")
        handler = self.handlers.get(action.action_type)
        if handler is None:
            raise ValidationError(f"Unsupported pending action type: {action.action_type}")
        try:
            # Handler stages its mutation in the same unit of work. Delete + mutation commit atomically.
            result = handler.execute(actor, action.arguments)
            self.repository.delete_pending(action.id)
            self.repository.commit()

            after_commit = getattr(handler, "after_commit", None)
            if callable(after_commit):
                try:
                    after_commit(actor, action.arguments, result)
                except Exception as exc:
                    # Post-commit side effects must never roll back
                    # an already-successful business transaction.
                    print(
                        f"Post-commit action failed for {action.action_type}: {exc}",
                        flush=True,
                    )

            return result
        except Exception:
            self.repository.rollback()
            raise

    def cancel(self, actor: AuthenticatedUser, session_id: str) -> None:
        self._verify_session_owner(actor, session_id)
        action = self.repository.get_pending(session_id, for_update=True)
        if action is None:
            raise NotFoundError("No action is waiting for confirmation in this conversation")
        if action.user_id != actor.user_id:
            raise AuthorizationError("This pending action belongs to another user")
        self.repository.delete_pending(action.id)
        self.repository.commit()

    def get(self, actor: AuthenticatedUser, session_id: str) -> PendingActionData | None:
        self._verify_session_owner(actor, session_id)
        action = self.repository.get_pending(session_id)
        if action is not None and action.user_id != actor.user_id:
            raise AuthorizationError("This pending action belongs to another user")
        return action

    def _verify_session_owner(self, actor: AuthenticatedUser, session_id: str) -> None:
        owner = self.repository.get_session_owner(session_id)
        if owner is None:
            raise NotFoundError("Conversation session was not found")
        if owner != actor.user_id:
            raise AuthorizationError("This conversation belongs to another user")

    def _is_expired(self, expires_at: datetime) -> bool:
        current = self.now()
        if expires_at.tzinfo is None:
            current = current.replace(tzinfo=None)
        return expires_at <= current

