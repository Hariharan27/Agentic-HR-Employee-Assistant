from app.core.config import Settings, get_settings
from app.main import app

PAYLOAD = {"sender": "it@example.com", "subject": "[Onboarding #1] done", "body": "Laptop is ready."}


def call(client, settings, headers=None):
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        return client.post("/api/v1/inbound/email", json=PAYLOAD, headers=headers or {})
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_inbound_email_is_disabled_without_a_configured_token(client):
    response = call(client, Settings(inbound_email_token=""), {"X-Inbound-Token": "anything"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"


def test_inbound_email_rejects_missing_or_wrong_token(client):
    settings = Settings(inbound_email_token="s3cret-token")

    assert call(client, settings).status_code == 401
    assert call(client, settings, {"X-Inbound-Token": "wrong"}).status_code == 401


def test_inbound_email_with_the_right_token_passes_authentication(client, monkeypatch):
    from app.api.routes import inbound_email

    class StopAfterAuth(Exception):
        pass

    def stop(*_args, **_kwargs):
        raise StopAfterAuth

    monkeypatch.setattr(inbound_email.InboundEmailProcessor, "extract_onboarding_request_id", stop)
    settings = Settings(inbound_email_token="s3cret-token")

    try:
        call(client, settings, {"X-Inbound-Token": "s3cret-token"})
    except StopAfterAuth:
        reached = True
    else:
        reached = False

    assert reached
