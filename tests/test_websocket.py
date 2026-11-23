from tests.conftest import signup


def general_id(client, person) -> int:
    return client.get("/api/rooms", headers=person["headers"]).json()[0]["id"]


def receive_until(ws, event_type: str) -> dict:
    """Read events until one of the given type arrives."""
    for _ in range(30):
        event = ws.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"No {event_type} event received")


def test_messages_are_delivered_saved_and_counted_as_unread(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)

    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_alice, \
         client.websocket_connect(f"/ws?token={bob['token']}") as ws_bob:
        receive_until(ws_alice, "online_users")
        receive_until(ws_bob, "online_users")
        presence = receive_until(ws_alice, "presence")  # alice sees bob come online
        assert presence["username"] == "bob" and presence["online"] is True

        ws_alice.send_json({"type": "message", "room_id": room_id, "content": " Hello Bob! ", "client_id": "c1"})
        received = receive_until(ws_bob, "message")
        assert received["message"]["content"] == "Hello Bob!"
        assert received["message"]["sender"]["username"] == "alice"
        echo = receive_until(ws_alice, "message")  # sender gets it back to confirm
        assert echo["client_id"] == "c1"

        rooms = client.get("/api/rooms", headers=bob["headers"]).json()
        assert rooms[0]["unread"] == 1 and rooms[0]["last_message"]["content"] == "Hello Bob!"

        ws_bob.send_json({"type": "read", "room_id": room_id, "message_id": received["message"]["id"]})
        ws_bob.send_json({"type": "ping"})
        receive_until(ws_bob, "pong")  # ensures the read was processed
        assert client.get("/api/rooms", headers=bob["headers"]).json()[0]["unread"] == 0

    history = client.get(f"/api/rooms/{room_id}/messages", headers=bob["headers"]).json()
    assert [m["content"] for m in history["messages"]] == ["Hello Bob!"]


def test_typing_goes_to_others_only(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_alice, \
         client.websocket_connect(f"/ws?token={bob['token']}") as ws_bob:
        receive_until(ws_bob, "online_users")
        ws_alice.send_json({"type": "typing", "room_id": room_id, "is_typing": True})
        typing = receive_until(ws_bob, "typing")
        assert typing["user"]["username"] == "alice" and typing["is_typing"] is True


def test_non_members_cannot_send(client):
    alice, bob, eve = signup(client, "alice"), signup(client, "bob"), signup(client, "eve")
    room = client.post("/api/rooms/direct/bob", headers=alice["headers"]).json()
    with client.websocket_connect(f"/ws?token={eve['token']}") as ws_eve:
        receive_until(ws_eve, "online_users")
        ws_eve.send_json({"type": "message", "room_id": room["id"], "content": "sneaky"})
        assert "not in this conversation" in receive_until(ws_eve, "error")["detail"]


def test_invalid_token_is_rejected(client):
    with client.websocket_connect("/ws?token=garbage") as ws:
        assert ws.receive_json()["type"] == "error"


def test_rate_limit(client):
    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        receive_until(ws, "online_users")
        for i in range(11):
            ws.send_json({"type": "message", "room_id": room_id, "content": f"spam {i}"})
        assert "too fast" in receive_until(ws, "error")["detail"]


def test_history_pagination(client):
    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        receive_until(ws, "online_users")
        for i in range(8):
            ws.send_json({"type": "message", "room_id": room_id, "content": f"m{i}"})
            receive_until(ws, "message")
    page = client.get(f"/api/rooms/{room_id}/messages?limit=5", headers=alice["headers"]).json()
    assert [m["content"] for m in page["messages"]] == ["m3", "m4", "m5", "m6", "m7"] and page["has_more"]
    older = client.get(f"/api/rooms/{room_id}/messages?limit=5&before_id={page['messages'][0]['id']}",
                       headers=alice["headers"]).json()
    assert [m["content"] for m in older["messages"]] == ["m0", "m1", "m2"] and not older["has_more"]


def test_signup_announces_new_member_of_general(client):
    alice = signup(client, "alice")
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        receive_until(ws, "online_users")
        signup(client, "bob")
        event = receive_until(ws, "member_joined")
        assert event["user"]["username"] == "bob"
