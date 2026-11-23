# 💬 Relay – Real-Time Chat App

A full-stack real-time chat application built with **FastAPI** and **WebSockets**.
Create an account, chat in groups or one-to-one, and see who's online and typing, live.

## ✨ Features

- **Accounts:** sign up and sign in with hashed passwords (bcrypt) and JWT login tokens
- **Careful sign-up and sign-in:** exact, field-by-field error messages, live username
  availability ("bob is already taken"), a live password checklist, confirm password,
  show/hide password, Caps Lock warning, and a short lock after 5 wrong passwords.
  Forms are cleared after signing out and when switching between sign in and sign up.
- **Real-time messaging** over WebSockets: messages appear instantly, no refresh
- **Group rooms:** create, browse, join and leave groups (everyone starts in *General*)
- **Direct messages:** private one-to-one chats
- **Online status:** live green dot when someone is online, across multiple tabs and devices
- **Typing indicators:** "alice is typing…", also shown in the chat header like WhatsApp
- **Read receipts:** ✓ sent, ✓✓ delivered, blue ✓✓ read (in groups, once everyone has read it)
- **Reply** to any message with a quote, and tap the quote to jump to the original
- **Edit** your messages within 15 minutes (marked "edited")
- **Delete for everyone** (your own messages, within 48 hours) leaves "This message was deleted";
  **delete for me** hides any message from your own chat only
- **Sounds:** a "sent" pop, an incoming chime, and a soft blip in the open chat, with an on/off switch
- **Desktop notifications** for new messages while the tab is in the background
- **Last seen:** "Last seen today at 4:45 PM" in direct chats
- **Unread counts** that persist, plus an unread total in the browser tab title
- **Photos, videos, voice messages and files**, like WhatsApp or Telegram:
  attach button, drag and drop, paste images, previews before sending, captions,
  upload progress bars, an in-chat photo viewer, video and audio players, and
  one-tap voice recording
- **Message history** with "Load older messages" (pagination)
- **Optimistic UI:** your message shows instantly and confirms when the server saves it
- **Auto-reconnect** with exponential backoff, catching up on anything missed while offline
- **Rate limiting** against spam (10 messages per 5 seconds)
- **XSS-safe rendering:** messages are always shown as plain text
- **Responsive** desktop and phone layouts
- **Themes:** System (follows your device), Light or Dark, on the sign-in page and in the app, remembered per browser
- **52 automated tests** (pytest) and **Docker** setup with PostgreSQL

## 🧱 Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Real-time | WebSockets |
| Database | SQLAlchemy 2.0 ORM, with SQLite (default) or PostgreSQL |
| Auth | bcrypt password hashing, JWT (PyJWT) |
| Frontend | HTML, CSS, vanilla JavaScript (no build step) |
| Testing | pytest, FastAPI TestClient |
| Deployment | Docker, Docker Compose |

## 📁 Project Structure

```
chat-app/
├── run.py                  # Start the server:  python run.py
├── app/
│   ├── main.py             # Creates the FastAPI app, connects routers
│   ├── config.py           # Settings (from .env)
│   ├── database.py         # Database engine & sessions
│   ├── models.py           # Tables: users, rooms, room_members, messages, attachments, hidden_messages
│   ├── schemas.py          # Input validation with clear messages (Pydantic)
│   ├── errors.py           # One error format: {"detail": ..., "errors": {field: message}}
│   ├── security.py         # Password hashing, JWT, "current user"
│   ├── services.py         # Shared logic: rooms, messages, unread counts, receipts
│   ├── migrations.py       # Adds new columns to older databases automatically
│   ├── connections.py      # Who's connected right now (WebSocket manager)
│   ├── storage.py          # Safe file saving: size limits, type checks
│   ├── routers/
│   │   ├── auth.py         # /api/auth  register, login, me
│   │   ├── users.py        # /api/users search
│   │   ├── rooms.py        # /api/rooms groups, DMs, history
│   │   ├── attachments.py  # file upload & download
│   │   ├── messages.py     # edit & delete messages
│   │   └── ws.py           # /ws        real-time events
│   └── static/             # Frontend: index.html, style.css, app.js
├── tests/                  # pytest tests
├── Dockerfile, docker-compose.yml
├── requirements.txt
└── .env.example
```

## 🔄 How It Works

```
Browser ──HTTP (REST)──► FastAPI ──► SQLAlchemy ──► SQLite / PostgreSQL
   │                        ▲
   └──── WebSocket ─────────┘
          │
          ▼
  ConnectionManager (user id -> open connections)
          │
          └─► pushes events to everyone in the room
```

1. **Sign in** over REST and receive a JWT token.
2. **Open a WebSocket** at `/ws?token=...`. The server checks the token and marks you online.
3. **Send** `{"type": "message", "room_id": 1, "content": "Hi"}`. The server checks membership and the rate limit, saves the message, then pushes it to every member's open connections.
4. **History** loads over REST (`GET /api/rooms/{id}/messages`), 50 messages at a time.
5. **Files** are uploaded over REST (`POST /api/rooms/{id}/attachments`, multipart form). The server saves the file, creates a message with the attachment, and pushes it over WebSockets like any other message.

### File sharing safety

- Files are saved under **random names**, so uploads can't overwrite each other or escape the folder (path traversal).
- The **size limit** (`MAX_UPLOAD_MB`, default 25) is enforced while streaming, so huge uploads stop early.
- Only known photo, video and audio formats (plus PDF) are **displayed inline**. Everything else, including HTML and SVG, is served as a download, with `X-Content-Type-Options: nosniff`, so uploaded files can't run scripts.
- Photos are verified by their **first bytes** (magic numbers), not just the claimed type.
- **Program files** (.exe, .bat, .ps1 and similar) are refused.
- **Only members** of a conversation can download its files. Outsiders get a 404, so they can't even tell the file exists.

### WebSocket events

| Browser → server | Server → browser |
|---|---|
| `message` {room_id, content, client_id, reply_to_id} | `message`: new message in one of your rooms |
| `typing` {room_id, is_typing} | `message_updated`: a message was edited or deleted for everyone |
| `delivered` {room_id, message_id} | `message_hidden`: you deleted a message "for me" (syncs your other tabs) |
| `read` {room_id, message_id} | `receipt`: someone received or read messages (updates ticks) |
| `ping` | `typing`, `presence` (with last seen), `online_users` |
| | `room_added`, `member_joined`, `member_left`, `error`, `pong` |

### Editing and deleting (REST)

| Request | What it does |
|---|---|
| `PATCH /api/messages/{id}` | Edit your own message (within `EDIT_WINDOW_MINUTES`) |
| `DELETE /api/messages/{id}?scope=everyone` | Delete your own message for everyone (within `DELETE_FOR_EVERYONE_HOURS`) |
| `DELETE /api/messages/{id}?scope=me` | Hide any message from your own chat |

### Account rules

| Field | Rules |
|---|---|
| Username | 3–20 characters; letters, numbers and `_`; must start with a letter; not case-sensitive |
| Password (sign-up) | 8–16 characters; at least one letter and one number; no spaces anywhere; can't contain the username |

The browser checks these as you type, and the server checks them again (browser checks can be
bypassed). Sign-in only requires that both fields are filled, so accounts created under older,
looser rules can still sign in.

Every error comes back in one format, so the page can show each message under its own field:

```json
{"detail": "Password must include at least one number.",
 "errors": {"password": "Password must include at least one number."}}
```

**Trade-off:** sign-in says whether the *username* or the *password* is wrong. Many apps hide
this to stop people discovering which usernames exist, but the sign-up page already reveals
that through the availability check. So instead, Relay locks a username for 10 minutes after
5 wrong passwords (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCK_MINUTES`), which stops password guessing.

### How the ticks work

Each membership stores two numbers: `last_delivered_message_id` and `last_read_message_id`.
When a message reaches someone's browser, it sends `delivered`. When they actually look at the
chat (tab visible and focused), it sends `read`. The server moves the marker forward and
broadcasts a `receipt`, and the sender's browser recalculates the ticks: grey ✓✓ when everyone
else's delivered marker has passed the message, and blue ✓✓ when everyone's read marker has.
Storing one number per person (instead of one row per message per person) keeps this fast.

## 🚀 Setup (Windows)

```bash
cd C:\Users\<you>\Projects\chat-app
py -3.12 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # then put a random SECRET_KEY in .env using below command
# Open .env and replace SECRET_KEY with a random value. You can generate one with:
python -c "import secrets; print(secrets.token_hex(32))" 
python run.py
```

Open **http://127.0.0.1:8000**. To test chatting, open a second browser window in private/incognito mode and sign up as a different user.

**Interactive API docs:** http://127.0.0.1:8000/docs (generated automatically by FastAPI)

### Chat from your phone (same Wi-Fi)
Set `HOST=0.0.0.0` in `.env`, restart, find your PC's IP with `ipconfig` (e.g. `192.168.1.5`),
and open `http://192.168.1.5:8000` on your phone. Allow Python through Windows Firewall if asked.

> Voice recording needs a secure page (`https://` or `localhost`), so the 🎤 button is hidden
> when you open the app by IP address. Photos, videos and files still work. Once deployed with
> HTTPS, voice messages work everywhere.

### Run the tests
```powershell
pytest
```

### Run with Docker + PostgreSQL
```powershell
docker compose up --build
```

## 🧠 Design Decisions

- **WebSocket token in the URL:** browsers can't send custom headers when opening a WebSocket, so the JWT goes in the query string over HTTPS.
- **REST for history, WebSocket for live events:** history is request/response data that benefits from pagination and caching, while live events need to be pushed.
- **`services.py`:** the REST API and WebSocket share the same rules (who can see what, how messages are saved).
- **Unread counts on the server:** each membership stores `last_read_message_id`, so counts survive reloads and work across devices.
- **Delete for everyone keeps a placeholder** (like WhatsApp), so replies quoting it still make sense, and it also deletes the shared file from disk for privacy.
- **Delete for me** is a separate `hidden_messages` table: the message stays for everyone else.
- **Sounds are generated in code** with the Web Audio API, so there are no audio files to load.
- **Automatic database upgrades:** `migrations.py` adds new columns to older databases. Larger projects use Alembic for this.
- **Incremental rendering:** new messages are appended rather than redrawing the list, so a playing video or voice note isn't interrupted.
- **Media URLs carry the token:** `<img>` and `<video>` tags can't send headers, so file links include the login token.
- **`client_id` for optimistic UI:** the browser tags each message so it can replace the "Sending…" version when the server confirms it.

## 📈 Scaling Further

`ConnectionManager` keeps connections in memory, which works for **one server process**.
To run several instances behind a load balancer, publish events to **Redis pub/sub** and have
every instance deliver them to its own connected users. Files would move from the local
`uploads/` folder to cloud storage like Amazon S3, with signed download URLs. Other next steps:
image thumbnails, virus scanning, database migrations with Alembic, emoji reactions,
forwarding, mute per chat, and push notifications to phones (via a service worker).

## 📤 Upload to GitHub

```bash
git init
git add .
git commit -m "Initial commit: Relay real-time chat app"
git branch -M main
git remote add origin https://github.com/<your-username>/chat-app.git
git push -u origin main
```
`.env`, the database file and `venv/` are in `.gitignore`, so they won't be uploaded.

## 📜 License
MIT
