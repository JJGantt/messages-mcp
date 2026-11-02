"""The messages server this machine's tools use, for the scripts beside this file.

Read from the `messages` MCP registration in ~/.claude.json, so a script always
reaches the server the tools do, with the token they already have, and neither
is stored a second time. MESSAGES_MCP_URL and MESSAGES_MCP_TOKEN override it,
which is how the scripts run on the Mac itself, where `messages` is registered
as stdio rather than HTTP.
"""

import json
import os
import pathlib
import sys


def registration():
    """(MCP endpoint URL, Authorization header value)."""
    if os.environ.get("MESSAGES_MCP_URL") and os.environ.get("MESSAGES_MCP_TOKEN"):
        return os.environ["MESSAGES_MCP_URL"], f"Bearer {os.environ['MESSAGES_MCP_TOKEN']}"
    config = pathlib.Path.home() / ".claude.json"
    entry = json.loads(config.read_text()).get("mcpServers", {}).get("messages")
    if not entry or entry.get("type") != "http":
        sys.exit(f"no HTTP `messages` MCP registered in {config}")
    return entry["url"], entry["headers"]["Authorization"]
