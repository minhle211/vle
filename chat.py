#!/usr/bin/env python3
"""Local chat window for VLe.

    py chat.py

Opens http://127.0.0.1:8765 . Each send runs the same pipeline as
`py vle.py`. Earlier turns in the window are included with the new question.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_TURNS = 24
MAX_CHARS = 8000
PAGE = agent.WORK_DIR / "chat.html"

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
RUNNING = threading.Lock()


def compose_question(messages: list[dict[str, str]]) -> str:
    current = messages[-1]["text"]
    prior = messages[:-1]
    if not prior:
        return current
    lines = ["Conversation so far:"]
    for turn in prior:
        speaker = "User" if turn["role"] == "user" else "VLe"
        lines.append(f"{speaker}: {turn['text']}")
    lines.append("")
    lines.append("Current question:")
    lines.append(current)
    return "\n".join(lines)


def clean_messages(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("Send the conversation as JSON.")
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Type a message first.")
    if len(raw) > MAX_TURNS:
        raw = raw[-MAX_TURNS:]
    messages: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each turn needs a role and text.")
        role = item.get("role")
        text = item.get("text")
        if role not in {"user", "vle"} or not isinstance(text, str):
            raise ValueError("Each turn needs a role and text.")
        text = text.strip()
        if not text:
            continue
        messages.append({"role": role, "text": text[:MAX_CHARS]})
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("The latest turn has to be your message.")
    return messages


def snapshot(job: dict) -> dict:
    with JOBS_LOCK:
        return {
            "id": job["id"],
            "status": job["status"],
            "stages": list(job["stages"]),
            "text": job["text"],
            "error": job["error"],
        }


def start_job(messages: list[dict[str, str]]) -> str | None:
    if not RUNNING.acquire(blocking=False):
        return None
    job = {
        "id": uuid.uuid4().hex,
        "status": "running",
        "stages": [],
        "text": "",
        "error": "",
    }

    def hook(name: str, text: str) -> None:
        with JOBS_LOCK:
            job["stages"].append({"name": name, "text": text})

    def work() -> None:
        token = agent.STAGE_HOOK.set(hook)
        try:
            answer = agent.run_pipeline(compose_question(messages), show=False)
            with JOBS_LOCK:
                job["text"] = answer
                job["status"] = "done"
        except Exception as err:
            with JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(err)
        finally:
            agent.STAGE_HOOK.reset(token)
            RUNNING.release()

    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=work, name="vle-chat", daemon=True).start()
    return job["id"]


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "VLeChat/1.0"

    def log_message(self, fmt: str, *args) -> None:
        if self.path.startswith("/api/messages/"):
            return
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            body = {"ok": True, "fake": agent.FAKE}
            self._send_json(200, body)
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        prefix = "/api/messages/"
        if path.startswith(prefix):
            job_id = path[len(prefix) :]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if job is None:
                self._send_json(404, {"error": "That reply is no longer in this session."})
                return
            self._send_json(200, snapshot(job))
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/messages":
            self._send_json(404, {"error": "Not found."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 400_000:
            self._send_json(400, {"error": "The message was empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            messages = clean_messages(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as err:
            message = str(err) if isinstance(err, ValueError) else "Send the conversation as JSON."
            self._send_json(400, {"error": message})
            return
        job_id = start_job(messages)
        if job_id is None:
            self._send_json(409, {"error": "VLe is still answering the previous message."})
            return
        self._send_json(202, {"id": job_id})

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(
            status,
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the VLe chat window.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not PAGE.exists():
        print(f"Missing {PAGE.name} next to chat.py.")
        return 1
    try:
        server = ThreadingHTTPServer((HOST, args.port), ChatHandler)
    except OSError as err:
        print(f"Could not listen on {HOST}:{args.port}. {err}")
        return 1
    url = f"http://{HOST}:{args.port}/"
    print(f"VLe chat is at {url}", flush=True)
    if agent.FAKE:
        print(
            "Offline fake model is on. Answers are canned and Cursor is not called.",
            flush=True,
        )
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
