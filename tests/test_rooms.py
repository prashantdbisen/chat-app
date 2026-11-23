from tests.conftest import signup


def test_new_users_join_general(client):
    alice = signup(client, "alice")
    rooms = client.get("/api/rooms", headers=alice["headers"]).json()
    assert [r["name"] for r in rooms] == ["General"]


def test_create_discover_join_and_leave_group(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room = client.post("/api/rooms", json={"name": "  Python   fans "}, headers=alice["headers"]).json()
    assert room["name"] == "Python fans"
    assert [m["username"] for m in room["members"]] == ["alice"]

    discover = client.get("/api/rooms/discover", headers=bob["headers"]).json()
    assert [(r["name"], r["member_count"]) for r in discover] == [("Python fans", 1)]

    joined = client.post(f"/api/rooms/{room['id']}/join", headers=bob["headers"]).json()
    assert [m["username"] for m in joined["members"]] == ["alice", "bob"]
    assert client.get("/api/rooms/discover", headers=bob["headers"]).json() == []

    assert client.post(f"/api/rooms/{room['id']}/leave", headers=bob["headers"]).status_code == 200
    assert len(client.get("/api/rooms", headers=bob["headers"]).json()) == 1  # only General


def test_direct_room_is_created_once(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    first = client.post("/api/rooms/direct/bob", headers=alice["headers"]).json()
    second = client.post("/api/rooms/direct/alice", headers=bob["headers"]).json()
    assert first["id"] == second["id"] and first["is_direct"] is True
    assert client.post("/api/rooms/direct/alice", headers=alice["headers"]).status_code == 400
    assert client.post("/api/rooms/direct/nobody", headers=alice["headers"]).status_code == 404


def test_outsiders_cannot_read_or_join_direct_messages(client):
    alice, bob, eve = signup(client, "alice"), signup(client, "bob"), signup(client, "eve")
    room = client.post("/api/rooms/direct/bob", headers=alice["headers"]).json()
    assert client.get(f"/api/rooms/{room['id']}/messages", headers=eve["headers"]).status_code == 403
    assert client.post(f"/api/rooms/{room['id']}/join", headers=eve["headers"]).status_code == 403


def test_user_search(client):
    alice = signup(client, "alice")
    signup(client, "bob")
    signup(client, "bobby")
    found = client.get("/api/users/search?q=BOB", headers=alice["headers"]).json()
    assert [u["username"] for u in found] == ["bob", "bobby"]
