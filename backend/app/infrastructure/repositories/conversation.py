import json

from sqlalchemy.orm import Session

from app.core.exceptions import AuthorizationError, ValidationError
from app.infrastructure.database.models import ConversationSession


class SQLAlchemyConversationRepository:
    def __init__(self, db: Session):
        self.db = db

    def load_or_create(self, session_id: str, user_id: int) -> dict[str, object]:
        row = self.db.get(ConversationSession, session_id)
        if row is None:
            row = ConversationSession(id=session_id, user_id=user_id, state_json="{}")
            self.db.add(row)
            self.db.commit()
            return {}
        if row.user_id != user_id:
            raise AuthorizationError("This conversation belongs to another user")
        try:
            state = json.loads(row.state_json)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Stored conversation state is invalid") from exc
        return state if isinstance(state, dict) else {}

    def save(self, session_id: str, user_id: int, state: dict[str, object]) -> None:
        row = self.db.get(ConversationSession, session_id)
        if row is None or row.user_id != user_id:
            raise AuthorizationError("This conversation belongs to another user")
        row.state_json = json.dumps(state, separators=(",", ":"), default=str)
        self.db.commit()
