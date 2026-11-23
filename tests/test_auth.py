import pytest

from tests.conftest import signup


def register(client, username, password):
    return client.post("/api/auth/register", json={"username": username, "password": password})


def login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_register_login_and_me(client):
    alice = signup(client, "Alice")
    assert alice["user"]["username"] == "Alice"  # shown as typed; signing in ignores case
    response = login(client, "  ALICE ", "secret123")
    assert response.status_code == 200
    assert client.get("/api/auth/me", headers=alice["headers"]).json()["username"] == "Alice"


@pytest.mark.parametrize("username, message", [
    ("", "Enter a username."),
    ("ab", "Username must be at least 3 characters."),
    ("a" * 21, "Username can be at most 20 characters."),
    ("john doe", "Username can't contain spaces."),
    ("john-doe", "Username can only use letters, numbers and underscores (_)."),
    ("2fast", "Username must start with a letter."),
    ("_alice", "Username must start with a letter."),
])
def test_username_rules_have_exact_messages(client, username, message):
    response = register(client, username, "secret123")
    assert response.status_code == 422
    assert response.json()["errors"]["username"] == message
    assert response.json()["detail"] == message


@pytest.mark.parametrize("password, message", [
    ("", "Enter a password."),
    ("abc123", "Password must be at least 8 characters."),
    ("onlyletters", "Password must include at least one number."),
    ("12345678", "Password must include at least one letter."),
    (" secret123", "Password can't contain spaces."),
    ("secret 123", "Password can't contain spaces."),
    ("secret123\t", "Password can't contain spaces."),
    ("Secret12345678901", "Password can be at most 16 characters."),  # 17 characters
    ("myfrank123", "Password can't contain your username."),
])
def test_password_rules_have_exact_messages(client, password, message):
    response = register(client, "frank", password)
    assert response.status_code == 422
    assert response.json()["errors"]["password"] == message


def test_password_length_limits_are_inclusive(client):
    assert register(client, "grace", "abcdef12").status_code == 201          # exactly 8
    assert register(client, "heidi", "abcdefgh12345678").status_code == 201  # exactly 16


def test_missing_fields(client):
    response = client.post("/api/auth/register", json={})
    assert response.json()["errors"] == {"username": "Enter a username.", "password": "Enter a password."}


@pytest.mark.parametrize("typed, shown", [
    ("Alice", "Alice"), ("alice", "alice"), ("ALICE", "Alice"), ("aLiCe", "alice"), ("Mr_Bean99", "Mr_bean99"),
])
def test_display_name_keeps_only_a_typed_first_capital(client, typed, shown):
    user = signup(client, typed)["user"]
    assert user["username"] == shown
    assert login(client, typed.lower(), "secret123").json()["user"]["username"] == shown
    assert register(client, typed.swapcase(), "secret123").status_code == 409  # still one account per name


def test_display_name_appears_everywhere(client):
    alice, bob = signup(client, "Alice"), signup(client, "Bob")
    assert [u["username"] for u in client.get("/api/users/search?q=bob", headers=alice["headers"]).json()] == ["Bob"]
    general = client.get("/api/rooms", headers=alice["headers"]).json()[0]
    assert [m["username"] for m in general["members"]] == ["Alice", "Bob"]
    dm = client.post("/api/rooms/direct/BOB", headers=alice["headers"]).json()  # any case works
    assert sorted(m["username"] for m in dm["members"]) == ["Alice", "Bob"]


def test_duplicate_username_says_it_is_taken(client):
    signup(client, "bob")
    response = register(client, "Bob", "another123")
    assert response.status_code == 409
    assert response.json()["errors"]["username"] == 'The username "bob" is already taken. Try another one.'


def test_username_availability_check(client):
    signup(client, "bob")
    taken = client.get("/api/auth/check-username?username=BOB").json()
    assert taken == {"username": "bob", "valid": True, "available": False,
                     "message": 'The username "bob" is already taken. Try another one.'}
    free = client.get("/api/auth/check-username?username=bob_99").json()
    assert free["available"] is True and free["message"] == '"bob_99" is available.'
    invalid = client.get("/api/auth/check-username?username=9lives").json()
    assert invalid["valid"] is False and invalid["message"] == "Username must start with a letter."


def test_login_says_exactly_what_is_wrong(client):
    signup(client, "carol")
    unknown = login(client, "nobody", "secret123")
    assert unknown.status_code == 401
    assert unknown.json()["errors"]["username"] == 'No account found with the username "nobody".'
    wrong = login(client, "carol", "wrongpass1")
    assert wrong.status_code == 401
    assert wrong.json()["errors"]["password"] == "Incorrect password."
    empty = login(client, "", "")
    assert empty.json()["errors"] == {"username": "Enter your username.", "password": "Enter your password."}


def test_too_many_wrong_passwords_lock_the_account_briefly(client):
    signup(client, "dave")
    messages = [login(client, "dave", f"wrong{i}aa").json()["detail"] for i in range(4)]
    assert messages[2] == "Incorrect password. 2 attempts left before a short lock."
    assert messages[3] == "Incorrect password. 1 attempt left before a short lock."
    fifth = login(client, "dave", "wrong5aa")
    assert fifth.status_code == 429 and "Try again in 10 minutes" in fifth.json()["detail"]
    assert login(client, "dave", "secret123").status_code == 429  # even the right password waits


def test_successful_login_resets_failed_attempts(client):
    signup(client, "erin")
    for _ in range(3):
        login(client, "erin", "nope12345")
    assert login(client, "erin", "secret123").status_code == 200
    assert login(client, "erin", "nope12345").json()["detail"] == "Incorrect password."


def test_protected_routes_need_a_valid_token(client):
    assert client.get("/api/rooms").status_code == 401
    bad = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/api/rooms", headers=bad).status_code == 401


def test_password_is_hashed(client):
    from app.database import SessionLocal
    from app.models import User

    signup(client, "erin", "mypassword1")
    with SessionLocal() as db:
        stored = db.query(User).filter_by(username="erin").one().password_hash
    assert "mypassword1" not in stored and stored.startswith("$2")


def test_group_name_messages(client):
    alice = signup(client, "alice")
    blank = client.post("/api/rooms", json={"name": "   "}, headers=alice["headers"])
    assert blank.json()["errors"]["name"] == "Enter a group name."
    long = client.post("/api/rooms", json={"name": "x" * 51}, headers=alice["headers"])
    assert long.json()["errors"]["name"] == "Group name can be at most 50 characters."
