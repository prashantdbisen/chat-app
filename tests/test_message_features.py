"""Ticks, replies, editing, deleting and last seen."""

from datetime import datetime, timedelta, timezone

from tests.conftest import signup
from tests.test_attachments import PNG, upload
from tests.test_websocket import general_id, receive_until


def send(ws, room_id, content, **extra):
    ws.send_json({"type": "message", "room_id": room_id, "content": content, **extra})
    return receive_until(ws, "message")["message"]


def test_ticks_sent_delivered_read(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    dm = client.post("/api/rooms/direct/bob", headers=alice["headers"]).json()
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_a, \
         client.websocket_connect(f"/ws?token={bob['token']}") as ws_b:
        receive_until(ws_a, "online_users")
        receive_until(ws_b, "online_users")
        message = send(ws_a, dm["id"], "Hi Bob")

        ws_b.send_json({"type": "delivered", "room_id": dm["id"], "message_id": message["id"]})
        receipt = receive_until(ws_a, "receipt")  # grey double tick
        assert receipt["user_id"] == bob["user"]["id"]
        assert receipt["delivered_id"] == message["id"] and receipt["read_id"] < message["id"]

        ws_b.send_json({"type": "read", "room_id": dm["id"], "message_id": message["id"]})
        receipt = receive_until(ws_a, "receipt")  # blue double tick
        assert receipt["read_id"] == message["id"]

        ws_b.send_json({"type": "read", "room_id": dm["id"], "message_id": 999999})  # can't read the future
        ws_b.send_json({"type": "ping"})
        receive_until(ws_b, "pong")
    room = client.get("/api/rooms", headers=alice["headers"]).json()
    bob_receipt = next(r for r in next(x for x in room if x["id"] == dm["id"])["receipts"]
                       if r["user_id"] == bob["user"]["id"])
    assert bob_receipt["read_id"] == message["id"]


def test_new_group_members_start_caught_up(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        receive_until(ws, "online_users")
        send(ws, room_id, "before carol joined")
    carol = signup(client, "carol")
    general = client.get("/api/rooms", headers=carol["headers"]).json()[0]
    assert general["unread"] == 0
    mine = next(r for r in general["receipts"] if r["user_id"] == carol["user"]["id"])
    assert mine["read_id"] == general["last_message"]["id"]


def test_reply_includes_a_quote(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={bob['token']}") as ws:
        receive_until(ws, "online_users")
        original = send(ws, room_id, "Pizza tonight?")
        reply = send(ws, room_id, "Or burgers", reply_to_id=original["id"])
        assert reply["reply_to"]["id"] == original["id"]
        assert reply["reply_to"]["content"] == "Pizza tonight?"
        bogus = send(ws, room_id, "reply to nothing", reply_to_id=123456)
        assert bogus["reply_to"] is None
    photo = upload(client, alice, room_id, "a.png", PNG, "image/png").json()
    response = client.post(f"/api/rooms/{room_id}/attachments", files={"file": ("b.png", PNG, "image/png")},
                           data={"reply_to_id": str(photo["id"])}, headers=bob["headers"]).json()
    assert response["reply_to"]["attachment_kind"] == "image"


def test_edit_own_message_only(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_a, \
         client.websocket_connect(f"/ws?token={bob['token']}") as ws_b:
        receive_until(ws_b, "online_users")
        message = send(ws_a, room_id, "Meet at 5")
        edited = client.patch(f"/api/messages/{message['id']}", json={"content": "Meet at 6"},
                              headers=alice["headers"])
        assert edited.status_code == 200 and edited.json()["edited"] is True
        update = receive_until(ws_b, "message_updated")
        assert update["message"]["content"] == "Meet at 6"

    assert client.patch(f"/api/messages/{message['id']}", json={"content": "hacked"},
                        headers=bob["headers"]).status_code == 403
    assert client.patch(f"/api/messages/{message['id']}", json={"content": "  "},
                        headers=alice["headers"]).status_code == 400


def test_edit_window_expires(client):
    from app.database import SessionLocal
    from app.models import Message

    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws:
        receive_until(ws, "online_users")
        message = send(ws, room_id, "old message")
    with SessionLocal() as db:
        db.get(Message, message["id"]).created_at = datetime.now(timezone.utc) - timedelta(days=3)
        db.commit()
    assert client.patch(f"/api/messages/{message['id']}", json={"content": "new"},
                        headers=alice["headers"]).status_code == 400
    too_old = client.delete(f"/api/messages/{message['id']}?scope=everyone", headers=alice["headers"])
    assert too_old.status_code == 400 and "for yourself" in too_old.json()["detail"]
    assert client.delete(f"/api/messages/{message['id']}?scope=me", headers=alice["headers"]).status_code == 200


def test_delete_for_everyone_removes_content_and_file(client):
    import app.storage
    from app.database import SessionLocal
    from app.models import Attachment

    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    photo = upload(client, alice, room_id, "private.png", PNG, "image/png").json()
    with SessionLocal() as db:
        stored = app.storage.file_path(db.get(Attachment, photo["attachment"]["id"]).stored_name)
    assert stored.exists()

    assert client.delete(f"/api/messages/{photo['id']}?scope=everyone",
                         headers=bob["headers"]).status_code == 403  # not bob's message
    with client.websocket_connect(f"/ws?token={bob['token']}") as ws_b:
        receive_until(ws_b, "online_users")
        response = client.delete(f"/api/messages/{photo['id']}?scope=everyone", headers=alice["headers"])
        assert response.json()["deleted"] is True
        update = receive_until(ws_b, "message_updated")["message"]
        assert update["deleted"] and update["content"] == "" and update["attachment"] is None

    assert not stored.exists()
    assert client.get(f"{photo['attachment']['url']}?token={bob['token']}").status_code == 404
    history = client.get(f"/api/rooms/{room_id}/messages", headers=bob["headers"]).json()["messages"]
    assert history[-1]["deleted"] is True  # placeholder stays: "This message was deleted"


def test_delete_for_me_hides_only_for_me(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_a:
        receive_until(ws_a, "online_users")
        keep = send(ws_a, room_id, "keep me")
        gone = send(ws_a, room_id, "embarrassing")
        assert client.delete(f"/api/messages/{gone['id']}?scope=me", headers=bob["headers"]).status_code == 200

    bob_view = client.get(f"/api/rooms/{room_id}/messages", headers=bob["headers"]).json()["messages"]
    alice_view = client.get(f"/api/rooms/{room_id}/messages", headers=alice["headers"]).json()["messages"]
    assert [m["content"] for m in bob_view] == ["keep me"]
    assert [m["content"] for m in alice_view] == ["keep me", "embarrassing"]
    bob_room = client.get("/api/rooms", headers=bob["headers"]).json()[0]
    assert bob_room["last_message"]["id"] == keep["id"]

    eve = signup(client, "eve")
    dm = client.post("/api/rooms/direct/alice", headers=bob["headers"]).json()
    with client.websocket_connect(f"/ws?token={bob['token']}") as ws_b:
        receive_until(ws_b, "online_users")
        secret = send(ws_b, dm["id"], "secret")
    assert client.delete(f"/api/messages/{secret['id']}?scope=me", headers=eve["headers"]).status_code == 404


def test_last_seen_is_saved_when_going_offline(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    with client.websocket_connect(f"/ws?token={alice['token']}") as ws_a:
        receive_until(ws_a, "online_users")
        with client.websocket_connect(f"/ws?token={bob['token']}"):
            receive_until(ws_a, "presence")
        offline = receive_until(ws_a, "presence")
        assert offline["online"] is False and offline["last_seen"].endswith("Z")
    general = client.get("/api/rooms", headers=alice["headers"]).json()[0]
    assert next(m for m in general["members"] if m["username"] == "bob")["last_seen"]


def test_public_config(client):
    config = client.get("/api/config").json()
    assert config["edit_window_minutes"] == 15 and config["delete_for_everyone_hours"] == 48

