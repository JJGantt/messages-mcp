#!/usr/bin/env python3
"""Serve the messages MCP over HTTP so the Pi and Fly can use it.

The tools themselves only work on this Mac — `chat.db` and AppleScript exist
nowhere else — but that is a constraint on where the *work* happens, not on where
the *caller* runs. Over HTTP the Mac is a data source rather than the machine
that has to be driving the agent, so a Pi-hosted session reads messages and sends
texts with no Mac-side model involved and no command routing.

Unlike `data-http`, this requires a bearer token. That service exposes life data;
this one exposes every message ever sent or received plus the ability to send new
ones as Jared, to anything that can reach the port. Matching its open-by-default
posture here would be the wrong default to copy.

Beside the MCP endpoint is `POST /upload`, behind the same token, which is how a
caller sends a file that exists only on its own machine: see `upload` below and
`bin/upload`.

    MESSAGES_MCP_TOKEN=<token> ./.venv/bin/python serve_http.py

Register it from another machine with:

    claude mcp add --transport http messages \\
      http://Jareds-MacBook-Air.local:27187/mcp \\
      --header "Authorization: Bearer <token>"
"""

import os
import pathlib
import secrets
import sys
import uuid

import uvicorn
from dotenv import load_dotenv
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from messages_mcp import OUTBOX, mcp

import threading
import time

import chatdb
import ops_report as ops

# Errors (anything logged at ERROR, which includes a failed tool call, and anything uncaught) go to the
# ops responder (health/responder) through a copy of its reporter (edit it there).
BEAT_S = 60


def beat_loop() -> None:
    """Beats only while chat.db can actually be read. The way this server fails is losing Full Disk
    Access: it keeps running and answering while every tool fails, so a beat on liveness alone would
    say all is well. A read that fails is reported and the beat stops."""
    while True:
        try:
            chatdb._query_one("select 1 from message limit 1")
            ops.beat(BEAT_S)
        except Exception as e:  # noqa: BLE001
            ops.report(f"chat.db unreadable: {e}")
        time.sleep(BEAT_S)

load_dotenv(pathlib.Path(__file__).parent / ".env")

PORT = int(os.environ.get("MESSAGES_MCP_PORT", 27187))
TOKEN = os.environ.get("MESSAGES_MCP_TOKEN", "")

# The transport enforces DNS-rebinding protection by validating the Host header,
# and trusts only localhost by default — so a request from the Pi arrives as
# "Invalid Host header" rather than anything auth-shaped. Naming the hostnames the
# Pi actually dials keeps that protection on instead of switching it off.
#
# The library matches the Host header exactly and case-sensitively, but clients
# do not preserve case: Claude Code's URL parser lowercases the hostname, so the
# Pi, registered as "Jareds-MacBook-Air.local", arrives as
# "jareds-macbook-air.local" and was refused with HTTP 421. Hostnames are
# case-insensitive, so each name is allowed as written and lowercased.
ALLOWED_HOSTS = sorted({
    form
    for h in os.environ.get(
        "MESSAGES_MCP_ALLOWED_HOSTS",
        f"Jareds-MacBook-Air.local:{PORT},localhost:{PORT},127.0.0.1:{PORT},"
        f"10.0.0.162:{PORT},100.106.101.57:{PORT}").split(",") if h.strip()
    for form in (h.strip(), h.strip().lower())
})


class BearerAuth(BaseHTTPMiddleware):
    """Reject anything without the shared token.

    Compared with `secrets.compare_digest` rather than `==` so the comparison
    does not leak the token's prefix through response timing.
    """

    async def dispatch(self, request, call_next):
        header = request.headers.get("authorization", "")
        offered = header[7:] if header.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(offered, TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def upload(request):
    """Take one file from another machine and answer with its path on this Mac.

    send_message takes paths, and a path on the Pi means nothing here, while a
    model cannot carry a file's bytes through a tool call. So the file comes over
    first, as the raw request body with its name in `?name=`, and the path in the
    reply is what the caller passes to send_message. It is written straight into
    OUTBOX, which Messages sends from in place, so the upload is the only copy
    ever made. One that is never sent stays there, unreferenced and inert.
    """
    name = pathlib.PurePath(request.query_params.get("name", "")).name
    if name in ("", ".", ".."):
        return JSONResponse({"error": "pass the file's name as ?name="}, status_code=400)
    target = OUTBOX / uuid.uuid4().hex / name
    target.parent.mkdir(parents=True)
    size = 0
    with open(target, "wb") as out:
        async for chunk in request.stream():
            out.write(chunk)
            size += len(chunk)
    if not size:
        target.unlink()
        target.parent.rmdir()
        return JSONResponse({"error": "empty upload"}, status_code=400)
    return JSONResponse({"path": str(target), "bytes": size})


def main() -> int:
    if not TOKEN:
        print("MESSAGES_MCP_TOKEN is unset — refusing to serve messages unauthenticated",
              file=sys.stderr)
        return 2

    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ALLOWED_HOSTS,
            allowed_origins=["*"],
        ))
    app.router.routes.append(Route("/upload", upload, methods=["POST"]))
    app.add_middleware(BearerAuth)
    ops.install(machine="mac", service="messages-http")
    threading.Thread(target=beat_loop, name="ops-beat", daemon=True).start()
    # Binds every interface on purpose: the whole point is reachability from the
    # Pi. The token, not the bind address, is what limits access.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
