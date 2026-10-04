from app.application.notifications.ports import EmailGateway, EmailMessage


class ConsoleEmailGateway(EmailGateway):
    """Development email gateway that writes emails to stdout."""

    def send(self, message: EmailMessage) -> None:
        recipients = ", ".join(message.to)

        print(
            "\n"
            "========== EMAIL ==========\n"
            f"To: {recipients}\n"
            f"Subject: {message.subject}\n"
            f"Body:\n{message.body}\n"
            "===========================\n",
            flush=True,
        )