from tests.conftest import signup
from tests.test_websocket import general_id, receive_until

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # starts like a real PNG file


def upload(client, person, room_id, name, data, content_type, caption=""):
    return client.post(
        f"/api/rooms/{room_id}/attachments",
        files={"file": (name, data, content_type)},
        data={"caption": caption, "client_id": "up1"},
        headers=person["headers"],
    )


def test_photo_is_shared_live_and_downloadable(client):
    alice, bob = signup(client, "alice"), signup(client, "bob")
    room_id = general_id(client, alice)
    with client.websocket_connect(f"/ws?token={bob['token']}") as ws_bob:
        receive_until(ws_bob, "online_users")
        response = upload(client, alice, room_id, "holiday.png", PNG, "image/png", caption="Beach day")
        assert response.status_code == 201
        event = receive_until(ws_bob, "message")

    attachment = event["message"]["attachment"]
    assert event["message"]["content"] == "Beach day"
    assert attachment["kind"] == "image" and attachment["name"] == "holiday.png"
    assert attachment["size"] == len(PNG)

    file = client.get(f"{attachment['url']}?token={bob['token']}")
    assert file.status_code == 200 and file.content == PNG
    assert file.headers["content-type"] == "image/png"
    assert file.headers["content-disposition"].startswith("inline")
    assert file.headers["x-content-type-options"] == "nosniff"

    history = client.get(f"/api/rooms/{room_id}/messages", headers=bob["headers"]).json()
    assert history["messages"][0]["attachment"]["id"] == attachment["id"]


def test_voice_note_and_document_kinds(client):
    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    voice = upload(client, alice, room_id, "voice-message.webm", b"\x1aE\xdf\xa3" + b"0" * 50,
                   "audio/webm;codecs=opus").json()
    assert voice["attachment"]["kind"] == "audio" and voice["attachment"]["content_type"] == "audio/webm"
    doc = upload(client, alice, room_id, "notes.docx", b"PK\x03\x04data", "application/vnd.openxmlformats").json()
    assert doc["attachment"]["kind"] == "file"


def test_outsiders_cannot_upload_or_download(client):
    alice, bob, eve = signup(client, "alice"), signup(client, "bob"), signup(client, "eve")
    dm = client.post("/api/rooms/direct/bob", headers=alice["headers"]).json()
    assert upload(client, eve, dm["id"], "x.png", PNG, "image/png").status_code == 403
    sent = upload(client, alice, dm["id"], "secret.png", PNG, "image/png").json()
    url = sent["attachment"]["url"]
    assert client.get(f"{url}?token={eve['token']}").status_code == 404
    assert client.get(url).status_code == 401
    assert client.get(f"{url}?token={bob['token']}").status_code == 200


def test_fake_image_and_html_are_downloads_not_inline(client):
    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    fake = upload(client, alice, room_id, "evil.png", b"<script>alert(1)</script>", "image/png").json()
    assert fake["attachment"]["kind"] == "file"
    html = upload(client, alice, room_id, "page.html", b"<script>alert(1)</script>", "text/html").json()
    response = client.get(f"{html['attachment']['url']}?token={alice['token']}")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")


def test_blocked_empty_and_oversized_files(client, monkeypatch):
    import app.storage

    alice = signup(client, "alice")
    room_id = general_id(client, alice)
    assert upload(client, alice, room_id, "virus.exe", b"MZ" + b"0" * 10, "application/x-msdownload").status_code == 400
    assert upload(client, alice, room_id, "empty.txt", b"", "text/plain").status_code == 400
    monkeypatch.setattr(app.storage, "MAX_UPLOAD_MB", 1)
    big = b"0" * (1024 * 1024 + 1)
    assert upload(client, alice, room_id, "big.bin", big, "application/octet-stream").status_code == 413
