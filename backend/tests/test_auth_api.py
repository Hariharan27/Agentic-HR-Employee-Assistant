def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_and_read_trusted_profile(client):
    login = client.post("/api/v1/auth/login", json={"username": "employee", "password": "correct-password"})
    assert login.status_code == 200
    body = login.json()
    assert body["role"] == "EMPLOYEE"

    profile = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert profile.status_code == 200
    assert profile.json()["employee_code"] == "E1"
    assert profile.json()["role"] == "EMPLOYEE"


def test_invalid_password_is_rejected(client):
    response = client.post("/api/v1/auth/login", json={"username": "employee", "password": "wrong-password"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_protected_endpoint_requires_jwt(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_protected_endpoint_rejects_invalid_jwt(client):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"

