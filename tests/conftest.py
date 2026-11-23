"""
Shared test setup: every test gets a fresh, empty database.
"""

import os
import shutil
import tempfile

# Must be set BEFORE the app is imported, so it uses a throwaway test database
_db_file = os.path.join(tempfile.gettempdir(), "relay_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["UPLOAD_DIR"] = os.path.join(tempfile.gettempdir(), "relay_test_uploads")
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    shutil.rmtree(os.environ["UPLOAD_DIR"], ignore_errors=True)  # fresh uploads folder
    from app.routers import auth as auth_routes
    auth_routes._failures.clear()  # forget failed sign-ins from other tests
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as test_client:  # "with" runs the app's startup code
        yield test_client


def signup(client, username: str, password: str = "secret123") -> dict:
    """Register a user and return {"token": ..., "user": ..., "headers": ...}."""
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    data = response.json()
    return {"token": data["access_token"], "user": data["user"],
            "headers": {"Authorization": f"Bearer {data['access_token']}"}}
