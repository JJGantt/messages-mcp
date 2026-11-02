#!/usr/bin/env python3
"""MCP server exposing macOS Messages (iMessage / SMS / RCS) to agents.

Every tool here is a thin wrapper. The work - opening chat.db, decoding
attributedBody, merging the two contact sources, driving AppleScript to send -
lives in the chatdb package, because anything else on this Mac that reads Messages needs
the same reading without going through an MCP client to reach a local file.
"""

import chatdb
from chatdb import ATTACHMENTS, CHAT_DB, OUTBOX, PLACEHOLDER  # re-exported: serve_http and
                                                             # bin/upload import them from here
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("messages")


@mcp.tool()
def list_chats(limit: int = 20, include_groups: bool = True) -> list[dict]:
    """List recent conversations, most recently active first.

    Args:
        limit: how many conversations to return.
        include_groups: include group chats as well as one-to-one threads."""
    return chatdb.list_chats(limit=limit, include_groups=include_groups)


@mcp.tool()
def get_thread(chat_id: int | None = None, contact: str | None = None,
               limit: int = 50) -> list[dict]:
    """Read messages from one conversation, oldest to newest.

    Messages from the user carry `status` (delivered, sent, failed, pending) and,
    when failed, Messages' `error` code.

    Args:
        chat_id: conversation id from list_chats or list_contacts.
        contact: alternatively a phone number or email; names are refused, so
            resolve a person through list_contacts first.
        limit: how many of the most recent messages to return."""
    return chatdb.get_thread(chat_id=chat_id, contact=contact, limit=limit)


@mcp.tool()
def search_messages(query: str, limit: int = 30, contact: str | None = None) -> list[dict]:
    """Search message text across all conversations, newest first.

    Covers messages whose content is only in the encoded blob as well as those
    with a plain text column, so nothing is silently missed. Matching is
    case-insensitive and happens in Python, walking newest-first and stopping at
    the limit; a full no-match scan of the whole database costs well under a
    second.

    Args:
        query: substring to look for.
        limit: how many matches to return.
        contact: optionally restrict to one phone number or email; names are
            refused, so resolve a person through list_contacts first."""
    return chatdb.search_messages(query=query, limit=limit, contact=contact)


@mcp.tool()
def list_contacts() -> dict:
    """Every contact's phone numbers and emails, most recently messaged first.

    This returns the whole address book rather than searching it, on purpose. A
    name to look up usually arrives through speech-to-text, so it is a guess at a
    spelling ("Mia" for "Maya"), and there may be several people it could mean.
    Choose the person by reading the list: the name that fits, weighed by who the
    user actually talks to. Ask the user when that still leaves more than one.

    Each row is one handle, so a person with a phone and an email appears twice.
    A row with `chat_id` has a one-to-one conversation: `last_message_at` and
    `messages` describe it, and `service` is the service of its latest message
    (iMessage, SMS or RCS). A row without one has never been messaged one-to-one.

    Send by passing the chosen row's `handle` to send_message as `to`."""
    return chatdb.list_contacts()


@mcp.tool()
def send_message(text: str | None = None, to: str | None = None,
                 chat_id: int | None = None, service: str | None = None,
                 files: list[str] | None = None) -> dict:
    """Send a message, files, or both from the user's own identity via Messages.app.

    Never pass a name. Resolve the person first, with list_contacts for one
    person or list_chats for a group, and pass exactly what it returned.

    `to` is a phone number or email. When a one-to-one conversation with it
    already exists, the message goes into that conversation and Messages picks
    iMessage or SMS itself, exactly as when the user types in that thread. With
    no conversation yet, a phone number needs `service` ("iMessage" or "SMS")
    because nothing here knows which it is on; an email can only be iMessage.
    Passing `service` with an existing conversation forces it, e.g. to retry as
    SMS after an iMessage send failed.

    `chat_id` (from list_chats) sends into that exact conversation, and is the
    only way to reach a group: a group has no single handle to address.

    `files` are absolute paths on the Mac this server runs on, including when it
    is called over HTTP from another machine. To send a file that exists only on
    the caller's machine, upload it first: on the Pi,
    `~/repos/messages-mcp/bin/upload <file>` prints the Mac path to pass here.
    Each goes as its own message, in order, before `text`. Every path is checked
    before anything is sent.

    The result's `status` is the weakest of its `parts`, each of which carries
    its own: `delivered`; `sent` (accepted but not yet delivered, which is final
    for SMS since it has no delivery receipt); `failed`, with Messages' `error`
    code, or with `detail` when a file's message arrived with no file attached;
    `pending` if still unsent, or a file still uploading, when the tool stopped
    waiting; or `unconfirmed` if no sent message appeared. get_thread shows the
    same status later.

    Requires the Mac awake and signed in. SMS is relayed through the iPhone, so
    that leg also needs the phone on.

    Args:
        text: message body, sent verbatim. Optional when files are given.
        to: phone number or email for a one-to-one message.
        chat_id: conversation id from list_chats; required for group chats.
        service: "iMessage" or "SMS"; see above for when it is needed.
        files: absolute paths on this Mac to send as attachments."""
    return chatdb.send_message(text=text, to=to, chat_id=chat_id, service=service, files=files)


if __name__ == "__main__":
    mcp.run()
