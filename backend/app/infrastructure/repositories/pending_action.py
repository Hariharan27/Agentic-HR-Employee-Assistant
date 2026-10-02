import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.pending.entities import PendingActionData
from app.core.exceptions import ConflictError
from app.infrastructure.database.models import ConversationSession, PendingAction


class SQLAlchemyPendingActionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_session_owner(self, session_id: str) -> int | None:
        return self.db.scalar(select(ConversationSession.user_id).where(ConversationSession.id == session_id))

    def get_pending(self, session_id: str, *, for_update: bool = False) -> PendingActionData | None:
        statement = select(PendingAction).where(PendingAction.session_id == session_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        return self._to_data(row) if row else None

    def add_pending(self, action: PendingActionData) -> PendingActionData:
        row = PendingAction(
            session_id=action.session_id,
            user_id=action.user_id,
            action_type=action.action_type,
            arguments_json=json.dumps(action.arguments, separators=(",", ":")),
            summary=action.summary,
            expires_at=action.expires_at,
        )
        self.db.add(row)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ConflictError("This conversation already has an action waiting for confirmation") from exc
        return self._to_data(row)

    def delete_pending(self, action_id: int) -> None:
        row = self.db.get(PendingAction, action_id)
        if row is not None:
            self.db.delete(row)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    @staticmethod
    def _to_data(row: PendingAction) -> PendingActionData:
        return PendingActionData(row.id, row.session_id, row.user_id, row.action_type,
                                 json.loads(row.arguments_json), row.summary, row.expires_at)
