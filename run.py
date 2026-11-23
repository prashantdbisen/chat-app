"""
run.py - Start the chat server.

    python run.py

Then open http://127.0.0.1:8000 in your browser.
Set HOST=0.0.0.0 in .env to let other devices on your Wi-Fi join.
"""

import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    print(f"\nChat server running - open http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=os.getenv("RELOAD", "true") == "true")
