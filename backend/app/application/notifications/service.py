from app.application.notifications.ports import EmailGateway, EmailMessage


class EmailService:
    """Application service for sending email notifications."""

    def __init__(self, gateway: EmailGateway) -> None:
        self._gateway = gateway

    def send(
        self,
        *,
        to: tuple[str, ...],
        subject: str,
        body: str,
    ) -> None:
        if not to:
            raise ValueError("At least one email recipient is required.")

        message = EmailMessage(
            to=to,
            subject=subject,
            body=body,
        )

        self._gateway.send(message)

    def send_safely(
        self,
        *,
        to: tuple[str, ...],
        subject: str,
        body: str,
    ) -> bool:
        try:
            self.send(
                to=to,
                subject=subject,
                body=body,
            )
            return True
        except Exception as exc:
            print(
                f"Email delivery failed "
                f"to={', '.join(to)} "
                f"subject={subject!r}: {exc}",
                flush=True,
            )
            return False