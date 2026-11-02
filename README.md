# messages-mcp

MCP server exposing macOS Messages (iMessage / SMS / RCS) to agents on this Mac.
Reads conversation history straight out of `~/Library/Messages/chat.db` and sends
through Messages.app via AppleScript.

## Requirements

**Full Disk Access**, granted to whatever process runs this server. Without it
chat.db will not open (`sqlite3.OperationalError: unable to open database file`)
and writes into Messages' Attachments folder fail with `PermissionError`, so
every tool fails.

- For a Claude Code session that is Terminal. The grant takes effect after the
  app restarts.
- For the launchd HTTP job it is **`bin/fda-launch`**, built from
  `launcher/fda-launch.c`. launchd starts it and it spawns the server as a
  child, which inherits its grant. Granting Homebrew's `Python.app` instead
  works only until the next `brew upgrade`, which changes that binary's path
  and signature and voids the grant with no error anywhere; the Pi's tools
  then fail while every local session keeps working. Rebuilding the launcher
  voids its grant the same way, so it is built once, is gitignored, and needs
  re-adding under Full Disk Access if it is ever rebuilt:

      clang -O2 -Wall -o bin/fda-launch launcher/fda-launch.c

The job definition is versioned in `launchd/`; install it with
`cp launchd/com.jaredgantt.messages-http.plist ~/Library/LaunchAgents/` and
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jaredgantt.messages-http.plist`.

**Automation access to Messages**, which macOS prompts for the first time a send
is attempted. Sending also needs the Mac awake and signed in.

Dependencies live in `.venv`, installed from `requirements.txt` plus `chatdb`, installed
editable from `~/repos/chatdb`.

## Tools

| Tool | What it does |
|---|---|
| `list_chats` | Recent conversations, most recently active first, with the last message's sender |
| `get_thread` | Messages in one conversation, by `chat_id` or handle, with delivery `status` on the user's own |
| `search_messages` | Case-insensitive substring search across all history |
| `list_contacts` | Every contact handle, most recently messaged first, with its conversation's date, count and service |
| `send_message` | Send text, files, or both as the user's own identity to a handle or into an existing chat, and report what Messages recorded |

## No tool takes a name

`list_contacts` returns the whole address book, and every tool that acts on a
person takes a phone number, email or `chat_id`. A name is refused.

That looks less helpful than a lookup, and it is deliberate. Names arrive through
speech-to-text, so they are guesses at a spelling ("Mia" for "Maya"), and one name
can mean several people: "Phil" is two contacts here, one texted ten thousand
times and the other never. Picking the person takes the whole list with recency
in view, which the calling model has and a matcher does not. A substring matcher
that sent on a single hit would deliver "text Al" to a Sally.

The list is ~211 rows, ~24KB. Cold it costs ~1s (the Google fetch), warm ~0.1s.

A row's `handle` is the conversation's own identifier when one exists, otherwise
the number as the contact card writes it, never the normalised key: normalising
keeps the last ten digits and so drops a non-US country code.

## Two things about chat.db that shape this code

**Message text is often not in the `text` column.** About a third of rows
(28.7k of 92.5k here) have `text` NULL and the real content in `attributedBody`,
an `NSAttributedString` serialised with the old typedstream format. This decodes
it through Apple's own `NSUnarchiver` rather than scraping the blob, so a format
change is Apple's problem rather than a parser rewrite. A reader that only
selects `text` silently loses a third of every conversation — including whole
threads, not a random sample.

The decode runs inside an explicit `objc.autorelease_pool()`, and that wrapper is
load-bearing rather than decoration. `NSData` and the unarchived string are
autoreleased, and a process with no Cocoa run loop never drains the pool, so
without it every message ever decoded stays resident — about 1.3 KB each, which
is gigabytes in a watcher that decodes the whole thread on every wakeup. The
`str()` copies are Python objects and outlive the pool, so nothing escapes.

**Senders are handles, not names.** `handle.id` holds raw phone numbers and email
addresses, matched against contacts on the last 10 digits because numbers are
stored inconsistently (`555-010-0123` and `+15550100199` both appear).

Names come from **two** sources merged, and both are load-bearing:

- the Mac's local AddressBook under `~/Library/Application Support/AddressBook`
  (note the top-level `AddressBook-v22.abcddb` is empty — real records live in
  `Sources/<uuid>/`)
- **Google Contacts** via the People API, reusing the refresh token `gmail-mcp`
  stores so the secret exists in one place. Set `GOOGLE_CREDS_ENV` to point
  elsewhere; missing credentials degrade to local-only rather than failing.

Sharing that token means anything that kills it kills name resolution here too,
and a Google password change kills it — every refresh token carrying Gmail scopes
is revoked. Because the fallback is silent by design, the symptom is phone
numbers where names used to be and no error anywhere. So a credential that is
*present but rejected* is now distinguished from one that was never configured:
it logs to stderr and `list_contacts` adds
`"google_contacts": {"status": "unavailable", ...}` to its result. Fix is to
re-run `gmail-mcp/authorize.py`.

Do not pin `scopes=` on the `Credentials` used to refresh. Naming a scope there
puts it in the refresh request, and Google rejects a refresh asking for anything
the token was not granted — so pinning `contacts.readonly` broke this the moment
`gmail-mcp` re-authorized as `contacts`. The token carries its own grant.

The merged table is cached, but the cache expires. These servers are long-lived
— the HTTP one had been up 29 hours — and a snapshot taken at launch survived
both a revoked Google token and a contact rename, still answering with a name
neither address book contained. A stale name is worse than a slow lookup because
nothing about it looks wrong. Local edits change an address book file, so mtime
and size catch them for free on every call; Google exposes nothing to watch, so
it gets `GOOGLE_TTL_SECONDS` (15 min). Cold rebuild is ~1s, a cache hit ~1ms.

Neither alone is enough. Measured on this machine: 27 handles locally, 195 in
Google, but **16 of the local ones are absent from Google** — so preferring
either source would silently lose names. Local wins a conflict, because that is
the name Messages.app itself displays and disagreeing with what is on screen is
worse than either name. Merged coverage took named handles from 14 to 100, and
frequent correspondents from 9/40 to 64/144.

`search_messages` therefore cannot push matching down into SQL, since a third of
the corpus is opaque to `LIKE`. It walks newest-first decoding as it goes and
stops at the limit. A full no-match scan of 92k messages costs ~0.6s.

## Reactions are messages, not properties

A tapback is its own row in `chat.db`, not a field on the message it reacts to. Its
`text` reads as a sentence — `Loved "Behind the pillar by pos6"` — which is
indistinguishable from someone typing that, and it quotes the original, so a reader
that treats it as an ordinary message both miscounts the thread and sees the quoted
content twice.

`get_thread` therefore returns `reaction` (`loved`, `liked`, `disliked`, `laughed`,
`emphasized`, `questioned`, or `emoji`), `reaction_removed`, and `reacts_to` — the id
of the message reacted to, or `None` when that message falls outside the page.

`reaction_removed` is not cosmetic. `associated_message_type` 3000–3005 is the same
reaction being *taken back*, and without the distinction a removal reads as though
someone reacted rather than un-reacted.

## Sending

Send is AppleScript, the only route that sends as the user's real identity.
Message text is passed as an `argv` argument rather than interpolated into the
script source, so message content can't break out into AppleScript.

Targeting is `to` (a phone number or email) or `chat_id` from `list_chats`.

A `to` with an existing one-to-one conversation is sent into that conversation by
its guid. One-to-one guids are `any;-;<handle>`, not tied to a service, so
Messages chooses iMessage or SMS at send time exactly as when typing in the
thread. Do not pick the service from `chat.service_name` instead: it goes stale,
and disagreed with the latest message's service in ~30 of ~1,500 threads here
(people who moved between iPhone and Android). A `to` with no conversation is
sent to a participant of a named account, so a phone number needs `service`; an
email can only be iMessage. An explicit `service` forces that account even when a
thread exists, which is how to retry as SMS after an iMessage failure.

**A group needs `chat_id`** — a group has no single handle to address, so it is
reached by its conversation guid (`any;+;<hex>`) rather than by participant.

**Success is read back, not assumed.** The AppleScript returns as soon as
Messages accepts the request, and an iMessage to a number not on iMessage returns
cleanly and then fails with `error` 22. So the tool polls chat.db for the new row
(matched on chat, text and time) until it is sent, delivered or failed, at most
`SEND_WAIT_SECONDS`, and returns that `status`. It does not wait for delivery:
iMessage delivery averaged hours over a month here (recipients' phones off), and
SMS and RCS rows almost never set `is_delivered`, so `sent` is final for them.
`get_thread` reports the same `status` on the user's messages, which is how to
check a slow delivery later.

**Files** go in `files` as absolute paths on this Mac, including when the caller
is the Pi over HTTP. A file that exists only on the caller's machine is uploaded
first (see *Serving it to the Pi and Fly*). Each is its own message, in order,
before `text`, and every path is checked before anything is sent. The result's
top-level `status` is the weakest of its `parts`, each of which carries its own.

Messages attaches only a file it can read, and that excludes most of the disk. A
file sent from `/tmp` or `~/repos` is not refused: the AppleScript returns
cleanly and Messages records an empty placeholder bubble with no file, marked
*delivered*. So a file outside `~/Library/Messages/Attachments` is first copied
into `~/Library/Messages/Attachments/messages-mcp/<uuid>/`, keeping its name,
which the recipient sees; one already inside it (an upload, or an attachment
someone sent) is sent as it is. **Those copies are permanent and must not be
cleaned up.** Messages sends a file inside its Attachments folder in place, so
the attachment row points at the staged copy, and deleting it breaks the
attachment in Messages' history.

A file part is read back by the attachment's name rather than by text, and
stays `pending` until the attachment's `transfer_state` reaches 5, because its
message row can read sent while the upload is still going. A placeholder row
with no attachment is reported as `failed` with a `detail` rather than believed.

Two constraints worth knowing before relying on it:

- **Blue-bubble iMessage** works from the Mac alone.
- **Green-bubble SMS** is relayed to the iPhone over Text Message Forwarding and
  transmitted by the *phone*, so that leg needs the phone powered on and in
  service as well as the Mac awake.

## Serving it to the Pi and Fly

The tools only work on this Mac, but that limits where the *work* happens, not
where the *caller* runs. `serve_http.py` exposes the same server over MCP's
streamable-HTTP transport as launchd job `com.jaredgantt.messages-http` on
**:27187**, so a Pi-hosted session reads messages and sends texts with no
Mac-side model involved and no command routing — the Mac is a data source.

    claude mcp add --transport http messages \
      http://Jareds-MacBook-Air.local:27187/mcp \
      --header "Authorization: Bearer $MESSAGES_MCP_TOKEN"

**A bearer token is required and the server refuses to start without one.**
`data-http` is deliberately not the model to copy here: it serves life data
unauthenticated on the LAN, while this endpoint exposes every message ever sent
or received *and* the ability to send new ones as Jared. The token lives in
`.env` (gitignored, mode 600) rather than in the plist.

Two things that will bite anyone changing this:

- The transport enforces **DNS-rebinding protection** by validating the `Host`
  header and trusts only localhost by default. A request from the Pi comes back
  as `Invalid Host header`, which looks nothing like an auth failure. The Mac's
  hostnames are named in `MESSAGES_MCP_ALLOWED_HOSTS`; the protection stays on.
- The Mac must be awake. This is a convenience for callers, not a removal of the
  Mac dependency — nothing here works with the lid shut.

**A file that lives on the caller's machine is uploaded first.** `send_message`
takes Mac paths, and a model cannot carry a file's bytes through a tool call, so
the bytes travel on their own: `POST /upload?name=<filename>` with the file as
the raw body, behind the same token, streams it into
`~/Library/Messages/Attachments/messages-mcp/<uuid>/` and answers
`{"path": ..., "bytes": ...}`. That `path` goes in `files`, and since it is
already inside Messages' folder it is sent without another copy.

On the Pi that is one command, printing one Mac path per line:

    ~/repos/messages-mcp/bin/upload <file>...

It reads the URL and token from the Pi's own `messages` registration in
`~/.claude.json`, so it always targets the server the tools use and the token is
stored nowhere new. It is a one-shot stdlib script that streams the file rather
than loading it, so it costs the Pi nothing between uses. An upload that is
never sent stays in that folder, unreferenced and inert.

**`bin/check` is the functional probe.** It calls `list_chats` through the same
registration and prints `ok`, or the error with a non-zero exit. A server that
answers `tools/list` proves nothing: without Full Disk Access the job runs,
authenticates and lists its tools while every call fails. Both scripts find the
server through `bin/registration.py`; on the Mac, where `messages` is registered
as stdio, point them at it with `MESSAGES_MCP_URL` and `MESSAGES_MCP_TOKEN`.

## Failures reach the ops responder

`serve_http.py` reports anything logged at ERROR (a failed tool call included) and anything uncaught to
the ops responder (health/responder) through the `ops-report` package (github.com/JJGantt/ops-report), pinned to a
tag in `requirements.txt`. Its heartbeat is functional: once a minute it reads `chat.db` and beats only if that worked, so
losing Full Disk Access (the server keeps running, every tool fails) stops the beat and is reported.

## Not in scope

Nothing is mirrored off this machine.

### chat.db connections are opened and closed in one place

The chat.db layer is the `chatdb` package (`~/repos/chatdb`), which opens a connection only inside
`_query()` and closes it there. An unclosed `sqlite3.Connection` is not freed by refcounting (it sits
in reference cycles), so a read that held one would leak descriptors and page cache over a 150 MB
`chat.db` — about ten descriptors a minute and gigabytes a day for a reader that rereads a thread
on every write Messages makes. `ps` does not show it: RSS stays near 150 MB while the compressor holds
the rest, so the real figure is `top -o mem` and its `MEM`/`CMPRS` columns.
