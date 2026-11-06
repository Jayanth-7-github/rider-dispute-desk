from __future__ import annotations
import sys
import threading
import time
from pathlib import Path
import uvicorn

# Add assignment directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from payswift.mock_server import Handler as PaySwiftHandler, ThreadingHTTPServer
from app.main import app
from evals.run import run_evals


def start_payswift(port=8081):
    server = ThreadingHTTPServer(("127.0.0.1", port), PaySwiftHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def start_app(port=8000):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server


if __name__ == "__main__":
    print("Starting PaySwift on :8081...")
    start_payswift(8081)
    print("Starting Dispute Desk on :8000...")
    start_app(8000)

    time.sleep(1.5)  # Wait for servers to bind
    print("Servers ready. Running evals suite...\n")

    try:
        run_evals("http://127.0.0.1:8000", "http://127.0.0.1:8081")
    except SystemExit as se:
        sys.exit(se.code)
