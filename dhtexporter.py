import sqlite3
import json
import argparse
import os
import sys
from typing import Dict, Any, Generator, List, Optional
from concurrent.futures import ThreadPoolExecutor
import threading

def parse_args():
    parser = argparse.ArgumentParser(description='Parse Discord History Tracker data to a local HTML file')
    parser.add_argument('sqlite_file', help='Path to DHT SQLite database file')
    parser.add_argument('--outdir', help='Output directory for HTML file', default='.')
    parser.add_argument('--dump-json', action='store_true', help='Dump JSON files separately (debug)')
    parser.add_argument('--threads', type=int, default=4, help='Number of threads to use for processing')
    return parser.parse_args()

# Thread-local storage for database connections
thread_local = threading.local()

def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Get a thread-local database connection."""
    if not hasattr(thread_local, "connection"):
        thread_local.connection = sqlite3.connect(db_path)
    return thread_local.connection

def close_db_connections():
    """Close all thread-local database connections."""
    if hasattr(thread_local, "connection"):
        thread_local.connection.close()
        del thread_local.connection

def print_counts(db_path: str):
    """Print counts of servers, channels and messages."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get server count
    cursor.execute("SELECT COUNT(*) FROM servers")
    server_count = cursor.fetchone()[0]

    # Get channel count
    cursor.execute("SELECT COUNT(*) FROM channels")
    channel_count = cursor.fetchone()[0]

    # Get message count
    cursor.execute("SELECT COUNT(*) FROM messages")
    message_count = cursor.fetchone()[0]

    conn.close()
    print(f"Servers: {server_count} - Channels: {channel_count} - Messages: {message_count}")

def fetch_metadata(db_path: str) -> Dict[str, Any]:
    """Fetch metadata from SQLite database and return as structured dictionary."""
    print("Parsing metadata...", end="", flush=True)
    conn = get_db_connection(db_path)
    cursor = conn.cursor()

    metadata = {
        "users": {},
        "servers": {},
        "channels": {}
    }

    # Fetch users data
    cursor.execute("SELECT id, name, display_name, avatar_url FROM users")
    for user_id, name, display_name, avatar_url in cursor.fetchall():
        user_data = {
            "name": name,
            "avatar": avatar_url
        }
        if display_name:
            user_data["displayName"] = display_name
        metadata["users"][str(user_id)] = user_data

    # Fetch servers data
    cursor.execute("SELECT id, name, type, icon_hash FROM servers")
    for server_id, name, server_type, icon_hash in cursor.fetchall():
        server_data = {
            "name": name,
            "type": server_type.lower(),
        }
        if icon_hash:
            server_data["iconUrl"] = f"https://cdn.discordapp.com/icons/{server_id}/{icon_hash}.webp"
        metadata["servers"][str(server_id)] = server_data

    # Fetch channels data
    cursor.execute("SELECT id, server, name FROM channels")
    for channel_id, server_id, name in cursor.fetchall():
        channel_data = {
            "server": str(server_id),
            "name": name
        }
        metadata["channels"][str(channel_id)] = channel_data

    print("Done")
    return metadata

def get_message_attachments(message_id: str) -> Optional[List[Dict]]:
    """Fetch attachments for a specific message."""
    conn = get_db_connection(args.sqlite_file)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT a.name, a.download_url, a.width, a.height
        FROM message_attachments ma
        JOIN attachments a ON ma.attachment_id = a.attachment_id
        WHERE ma.message_id = ?
    """, (message_id,))

    attachments = []
    for name, url, width, height in cursor.fetchall():
        attachment = {
            "url": url,
            "name": name
        }
        if width and height:
            attachment.update({
                "width": width,
                "height": height
            })
        attachments.append(attachment)

    return attachments if attachments else None

def get_message_edit_timestamp(message_id: str) -> Optional[int]:
    """Fetch edit timestamp for a specific message."""
    conn = get_db_connection(args.sqlite_file)
    cursor = conn.cursor()

    cursor.execute("SELECT edit_timestamp FROM message_edit_timestamps WHERE message_id = ?", (message_id,))
    result = cursor.fetchone()
    return result[0] if result else None

def get_message_embeds(message_id: str) -> Optional[List[str]]:
    """Fetch embeds for a specific message."""
    conn = get_db_connection(args.sqlite_file)
    cursor = conn.cursor()

    cursor.execute("SELECT json FROM message_embeds WHERE message_id = ?", (message_id,))
    embeds = [row[0] for row in cursor.fetchall()]
    return embeds if embeds else None

def get_message_reactions(message_id: str) -> Optional[List[Dict]]:
    """Fetch reactions for a specific message."""
    conn = get_db_connection(args.sqlite_file)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT emoji_id, emoji_name, emoji_flags, count
        FROM message_reactions
        WHERE message_id = ?
    """, (message_id,))

    reactions = []
    for emoji_id, emoji_name, emoji_flags, count in cursor.fetchall():
        reaction = {
            "n": emoji_name,
            "a": bool(emoji_flags),  # True if animated emoji
            "c": count
        }
        if emoji_id:
            reaction["id"] = str(emoji_id)
        reactions.append(reaction)

    return reactions if reactions else None

def get_message_reply(message_id: str) -> Optional[str]:
    """Fetch replied-to message ID for a specific message."""
    conn = get_db_connection(args.sqlite_file)
    cursor = conn.cursor()

    cursor.execute("SELECT replied_to_id FROM message_replied_to WHERE message_id = ?", (message_id,))
    result = cursor.fetchone()
    return str(result[0]) if result else None

def should_include_message_text(text: str, attachments: Optional[List], embeds: Optional[List]) -> bool:
    """Determine if we should include the message text field."""
    # Always include if there's text
    if text:
        return True
    # Don't include if there are attachments or embeds
    if attachments or embeds:
        return False
    # Include empty string if nothing else
    return True

def process_message(message_data: tuple) -> str:
    """Process a single message into its final JSON format."""
    message_id, sender_id, channel_id, text, timestamp, idx, total = message_data
    message_id_str = str(message_id)

    # Print progress
    print(f"\rParsing messages {idx + 1} of {total}...", end="", flush=True)

    # Get all message components first
    attachments = get_message_attachments(message_id_str)
    embeds = get_message_embeds(message_id_str)
    edit_timestamp = get_message_edit_timestamp(message_id_str)
    reactions = get_message_reactions(message_id_str)
    reply_to = get_message_reply(message_id_str)

    # Base message structure
    message_obj = {
        "id": message_id_str,
        "c": str(channel_id),
        "u": str(sender_id),
        "t": timestamp
    }

    # Conditionally include message text
    if should_include_message_text(text, attachments, embeds):
        message_obj["m"] = text or ""

    # Add attachments if they exist
    if attachments:
        message_obj["a"] = attachments

    # Add embeds if they exist
    if embeds:
        message_obj["e"] = embeds

    # Add edit timestamp if it exists
    if edit_timestamp:
        message_obj["te"] = edit_timestamp

    # Add reactions if they exist
    if reactions:
        message_obj["re"] = reactions

    # Add reply reference if it exists
    if reply_to:
        message_obj["r"] = reply_to

    # All messages parsed, it's done
    if idx + 1 == total:
        print("Done")

    return json.dumps(message_obj, ensure_ascii=False)

def generate_messages_ndjson(db_path: str, num_threads: int = 4) -> Generator[str, None, None]:
    """Generate x-ndjson formatted messages from SQLite database using threading."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all messages first for multithreading
    cursor.execute("SELECT message_id, sender_id, channel_id, text, timestamp FROM messages ORDER BY timestamp")
    all_messages = cursor.fetchall()
    total_messages = len(all_messages)
    conn.close()

    # Add index and total to each message for progress tracking
    messages_with_progress = [(*msg, idx, total_messages) for idx, msg in enumerate(all_messages)]

    # Process messages in parallel
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        for result in executor.map(process_message, messages_with_progress):
            yield result

    close_db_connections()

def main():
    global args
    args = parse_args()

    # Print initial counts
    print_counts(args.sqlite_file)

    # Ensure output directory exists
    os.makedirs(args.outdir, exist_ok=True)

    # Base name for output files
    base_name = os.path.splitext(os.path.basename(args.sqlite_file))[0]

    # Fetch and format metadata
    metadata_json = json.dumps(fetch_metadata(args.sqlite_file), indent=2)

    # Generate messages in x-ndjson format (multi-threaded)
    messages_ndjson = "\n".join(generate_messages_ndjson(args.sqlite_file, args.threads))

    # Save JSON files if requested
    if args.dump_json:
        metadata_path = os.path.join(args.outdir, "get-viewer-metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            f.write(metadata_json)

        messages_path = os.path.join(args.outdir, "get-viewer-messages.ndjson")
        with open(messages_path, "w", encoding="utf-8") as f:
            f.write(messages_ndjson)

        print(f"Saved metadata to {metadata_path}")
        print(f"Saved messages to {messages_path}")

    # Generate HTML file
    html_path = os.path.join(args.outdir, f"{base_name}.html")

    final_html = (
        html_template
        .replace("//__METADATA__", metadata_json)
        .replace("//__MESSAGES__", messages_ndjson)
        .replace("//__STYLE__", style)
        .replace("//__SCRIPT__", script)
    )

    with open(html_path, "w", encoding="utf-8") as file:
        file.write(final_html)
    print(f"HTML generated successfully at {html_path}")


# Only template definitions below this line
html_template = r"""
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="referrer" content="no-referrer">

    <title>Discord Offline History</title>

    <!--link rel="icon" href="favicon.ico"-->

    <!-- Embed JSON metadata -->
    <script id="viewer-metadata" type="application/json">
    //__METADATA__
    </script>

    <!-- Embed JSON messages -->
    <script id="viewer-messages" type="application/x-ndjson">
    //__MESSAGES__
    </script>

    <script type="text/javascript">
		const query = new URLSearchParams(location.search);
		window.DHT_SERVER_TOKEN = query.get("token");
		window.DHT_SERVER_SESSION = query.get("session");
    </script>

    <!-- Embed Viewer CSS -->
    <style>
    //__STYLE__
    </style>

    <!-- Embed Viewer JS -->
    <script type="text/javascript">
    //__SCRIPT__
    </script>
  </head>
  <body>
    <div id="menu">
      <button id="btn-settings">Settings</button>

      <div class="splitter"></div>

      <div> <!-- needed to stop the select from messing up -->
        <select id="opt-messages-per-page">
          <option value="50">50 messages per page&nbsp;</option>
          <option value="100">100 messages per page&nbsp;</option>
          <option value="250">250 messages per page&nbsp;</option>
          <option value="500">500 messages per page&nbsp;</option>
          <option value="1000">1000 messages per page&nbsp;</option>
          <option value="0">All messages&nbsp;</option>
        </select>
      </div>

      <div class="nav">
        <button id="nav-first" data-nav="first" class="icon">&laquo;</button>
        <button id="nav-prev" data-nav="prev" class="icon">&lsaquo;</button>
        <button id="nav-pick" data-nav="pick">Page <span id="nav-page-current">1</span>/<span id="nav-page-total">?</span></button>
        <button id="nav-next" data-nav="next" class="icon">&rsaquo;</button>
        <button id="nav-last" data-nav="last" class="icon">&raquo;</button>
      </div>

      <div class="splitter"></div>

      <div> <!-- needed to stop the select from messing up -->
        <select id="opt-messages-filter">
          <option value="">No filter&nbsp;</option>
          <option value="user">Filter messages by user&nbsp;</option>
          <option value="contents">Filter messages by contents&nbsp;</option>
          <option value="withimages">Only messages with images&nbsp;</option>
          <option value="withdownloads">Only messages with downloads&nbsp;</option>
          <option value="edited">Only edited messages&nbsp;</option>
        </select>
      </div>

      <div id="opt-filter-list">
        <select id="opt-filter-user" data-filter-type="user">
          <option value="">Select user...</option>
        </select>
        <input id="opt-filter-contents" type="text" data-filter-type="contents" placeholder="Messages containing...">
        <input type="hidden" data-filter-type="withimages" value="1">
        <input type="hidden" data-filter-type="withdownloads" value="1">
        <input type="hidden" data-filter-type="edited" value="1">
      </div>

      <div class="separator"></div>

      <button id="btn-about">About</button>
    </div>

    <div id="app">
      <div id="channels">
        <div class="loading"></div>
      </div>
      <div id="messages"></div>
    </div>

    <div id="modal">
      <div id="overlay"></div>
      <div id="dialog"></div>
    </div>
  </body>
</html>
"""

style = r"""
/* styles/main.css */
body {
  font-family:
    Whitney,
    "Helvetica Neue",
    Helvetica,
    Verdana,
    "Lucida Grande",
    sans-serif;
  line-height: 1;
  margin: 0;
  padding: 0;
  overflow: hidden;
}
#app {
  height: calc(100vh - 48px);
  display: flex;
  flex-direction: row;
}
.loading {
  position: relative;
  --loading-backdrop: rgba(0, 0, 0, 0);
}
.loading::after {
  content: "";
  background: var(--loading-backdrop) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 300' preserveAspectRatio='xMidYMid'%3E %3Ccircle cx='150' cy='150' fill='none' stroke='%237983f5' stroke-width='8' r='42' stroke-dasharray='198 68'%3E %3CanimateTransform attributeName='transform' type='rotate' repeatCount='indefinite' dur='1.25s' values='0 150 150;360 150 150' keyTimes='0;1' /%3E %3C/circle%3E %3C/svg%3E") no-repeat center center;
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
}

/* styles/menu.css */
#menu {
  display: flex;
  flex-direction: row;
  align-items: stretch;
  gap: 8px;
  padding: 8px;
  background-color: #17181c;
  border-bottom: 1px dotted #5d626b;
}
#menu .splitter {
  flex: 0 0 1px;
  margin: 9px 1px;
  background-color: #5d626b;
}
#menu .separator {
  flex: 1 1 0;
}
#menu :disabled {
  background-color: #555;
  cursor: default;
}
#menu button,
#menu select,
#menu input[type=text] {
  height: 31px;
  padding: 0 10px;
  background-color: #7289da;
  color: #fff;
  text-shadow: 1px 1px 2px rgba(0, 0, 0, 0.75);
}
#menu button {
  font-size: 17px;
  border: 0;
  cursor: pointer;
  white-space: nowrap;
}
#menu select {
  font-size: 14px;
  border: 0;
  cursor: pointer;
}
#menu input[type=text] {
  font-size: 14px;
  border: 0;
}
#menu .nav {
  display: flex;
  flex-direction: row;
}
#menu .nav > button {
  font-size: 14px;
}
#menu .nav > button.icon {
  font-family: Lucida Console, monospace;
  font-size: 17px;
  padding: 0 8px;
}
#menu .nav > button,
#menu .nav > p {
  margin: 0 1px;
}
#opt-filter-list > select,
#opt-filter-list > input {
  display: none;
}
#opt-filter-list > .active {
  display: block;
}
#btn-about {
  margin-left: auto;
}

/* styles/channels.css */
#channels {
  width: 15vw;
  min-width: 215px;
  max-width: 300px;
  overflow-y: auto;
  color: #eee;
  background-color: #1c1e22;
  font-size: 15px;
}
#channels > div.loading {
  margin: 0 auto;
  width: 150px;
  height: 150px;
}
#channels > div.channel {
  cursor: pointer;
  padding: 10px 12px;
  border-bottom: 1px solid #333333;
}
#channels > div.channel:hover,
#channels > div.channel.active {
  background-color: #282b30;
}
#channels .info {
  display: flex;
  height: 16px;
  margin-bottom: 4px;
}
#channels .name {
  flex-grow: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow: hidden;
}
#channels .tag {
  flex-shrink: 1;
  background-color: rgba(255, 255, 255, 0.08);
  border-radius: 4px;
  margin-left: 4px;
  margin-top: 1px;
  padding: 2px 5px;
  font-size: 11px;
}

/* styles/messages.css */
#messages {
  flex: 1 1 0;
  overflow-y: auto;
  background-color: #36393e;
}
#messages > div {
  margin: 0 24px;
  padding: 4px 0 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
#messages h2 {
  margin: 0;
  padding: 0;
  display: block;
}
#messages .avatar-wrapper {
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  align-content: flex-start;
}
#messages .avatar-wrapper > div {
  flex: 1 1 auto;
}
#messages .avatar {
  flex: 0 0 38px !important;
  margin: 8px 14px 0 0;
}
#messages .avatar img {
  width: 38px;
  border-radius: 50%;
}
#messages .username {
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  margin-right: 3px;
  letter-spacing: 0;
}
#messages .info {
  color: rgba(255, 255, 255, 0.4);
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0;
}
#messages .info::before {
  content: "\2022";
  text-align: center;
  display: inline-block;
  width: 14px;
}
#messages .jump {
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
}
.message {
  margin-top: 6px;
  color: rgba(255, 255, 255, 0.7);
  font-size: 15px;
  line-height: 1.1em;
  white-space: pre-wrap;
  word-wrap: break-word;
}
.message .link,
.reply-message .link {
  color: #7289da;
  background-color: rgba(115, 139, 215, 0.1);
}
.message a,
.reply-message a {
  color: #0096cf;
  text-decoration: none;
}
.message a:hover {
  text-decoration: underline;
}
.message p {
  margin: 0;
}
.message .embed {
  display: inline-block;
  margin-top: 8px;
}
.message .embed .title {
  font-weight: bold;
  display: inline-block;
}
.message .embed .desc {
  margin-top: 4px;
}
.message .thumbnail {
  --loading-backdrop: rgba(0, 0, 0, 0.75);
  max-width: calc(100% - 20px);
  max-height: 320px;
}
.message .thumbnail img {
  width: auto;
  max-width: 100%;
  max-height: 320px;
  border-radius: 3px;
}
.message .download {
  margin-right: 8px;
  padding: 8px 9px;
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: 3px;
}
.message .embed:first-child,
.message .download + .download {
  margin-top: 0;
}
.message code {
  background-color: #2e3136;
  border-radius: 5px;
  font-family:
    Menlo,
    Consolas,
    Monaco,
    monospace;
  font-size: 14px;
}
.message code.inline {
  display: inline;
  padding: 2px;
}
.message code.block {
  display: block;
  border: 2px solid #282b30;
  margin-top: 6px;
  padding: 7px;
}
.message .emoji {
  width: 22px;
  height: 22px;
  margin: 0 1px;
  vertical-align: -30%;
  object-fit: contain;
}
.reply-message {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  line-height: 120%;
  white-space: nowrap;
}
.reply-message-with-avatar {
  margin: 0 0 -2px 52px;
}
.reply-message .jump {
  color: rgba(255, 255, 255, 0.4);
  font-size: 12px;
  text-underline-offset: 1px;
  margin-right: 7px;
}
.reply-message .emoji {
  width: 16px;
  height: 16px;
  vertical-align: -20%;
  object-fit: contain;
}
.reply-message .user {
  margin-right: 5px;
}
.reply-avatar {
  margin-right: 4px;
}
.reply-avatar img {
  width: 16px;
  border-radius: 50%;
  vertical-align: middle;
}
.reply-username {
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0;
}
.reply-contents {
  display: inline-block;
  color: rgba(255, 255, 255, 0.7);
  font-size: 12px;
  max-width: calc(80%);
}
.reply-contents p {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.reply-contents code {
  background-color: #2e3136;
  font-family:
    Menlo,
    Consolas,
    Monaco,
    monospace;
  padding: 1px 2px;
}
.reply-missing {
  color: rgba(255, 255, 255, 0.55);
}
.reactions {
  margin-top: 4px;
}
.reactions .reaction-wrapper {
  display: inline-block;
  border-radius: 4px;
  margin: 3px 2px 0 0;
  padding: 3px 6px;
  background: #42454a;
  cursor: default;
}
.reactions .reaction-emoji {
  margin-right: 5px;
  font-size: 16px;
  display: inline-block;
  text-align: center;
  vertical-align: -5%;
}
.reactions .reaction-emoji-custom {
  height: 15px;
  margin-right: 5px;
  vertical-align: -10%;
}
.reactions .count {
  color: rgba(255, 255, 255, 0.45);
  font-size: 14px;
}

/* styles/modal.css */
#modal div {
  position: absolute;
  display: none;
}
#modal.visible div {
  display: block;
}
#modal #overlay {
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  background-color: #000;
}
#modal.visible #overlay {
  opacity: 0.5;
}
#dialog {
  left: 50%;
  top: 50%;
  padding: 16px;
  background-color: #fff;
  transform: translateY(-50%);
}
#dialog p {
  line-height: 1.2;
}
#dialog p:first-child,
#dialog p:last-child {
  margin-top: 1px;
  margin-bottom: 1px;
}
#dialog a {
  color: #0096cf;
  text-decoration: none;
}
#dialog a:hover {
  text-decoration: underline;
}

/* bundle.css */
"""

script = r"""
// scripts/dom.mjs
var HTML_ENTITY_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
};
var HTML_ENTITY_REGEX = /[&<>"']/g;
var dom_default = class {
  /**
   * Returns a child element by its ID. Parent defaults to the entire document.
   * @param {string} id
   * @param {HTMLElement} [parent]
   * @returns {HTMLElement|null}
   */
  static id(id, parent) {
    return (parent || document).getElementById(id);
  }
  /**
   * Returns an array of all child elements containing the specified class. Parent defaults to the entire document.
   * @param {string} cls
   * @param {HTMLElement} [parent]
   * @returns {HTMLElement[]}
   */
  static cls(cls, parent) {
    return Array.prototype.slice.call((parent || document).getElementsByClassName(cls));
  }
  /**
   * Returns an array of all child elements that have the specified tag. Parent defaults to the entire document.
   * @param {string} tag
   * @param {HTMLElement} [parent]
   * @returns {HTMLElement[]}
   */
  static tag(tag, parent) {
    return Array.prototype.slice.call((parent || document).getElementsByTagName(tag));
  }
  /**
   * Returns the first child element containing the specified class. Parent defaults to the entire document.
   * @param {string} cls
   * @param {HTMLElement} [parent]
   * @returns {HTMLElement}
   */
  static fcls(cls, parent) {
    return (parent || document).getElementsByClassName(cls)[0];
  }
  /**
   * Converts characters to their HTML entity form.
   */
  static escapeHTML(html) {
    return String(html).replace(HTML_ENTITY_REGEX, (s) => HTML_ENTITY_MAP[s]);
  }
  /**
   * Converts a timestamp into a human readable time, using the browser locale.
   */
  static getHumanReadableTime(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleDateString() + ", " + date.toLocaleTimeString();
  }
  /**
   * Parses a url string into a URL object and returns it. If the parsing fails, returns null.
   */
  static tryParseUrl(url) {
    try {
      return new URL(url);
    } catch (ignore) {
      return null;
    }
  }
};

// scripts/template.mjs
var TEMPLATE_REGEX = /{([^{}]+?)}/g;
var template_default = class {
  constructor(contents) {
    this.contents = contents;
  }
  apply(obj, processor) {
    return this.contents.replace(TEMPLATE_REGEX, (full, match) => {
      const value = match.split(".").reduce((o, property) => o[property], obj);
      if (processor) {
        const updated = processor(match, value);
        return typeof updated === "undefined" ? dom_default.escapeHTML(value) : updated;
      }
      return dom_default.escapeHTML(value);
    });
  }
};

// scripts/settings.mjs
var settings_default = function() {
  const root = {
    onSettingsChanged(callback) {
      settingsChangedEvents.push(callback);
    }
  };
  const settingsChangedEvents = [];
  const triggerSettingsChanged = function(property) {
    for (const callback of settingsChangedEvents) {
      callback(property);
    }
  };
  const getStorageItem = (property) => {
    try {
      return localStorage.getItem(property);
    } catch (e) {
      console.error(e);
      return null;
    }
  };
  const setStorageItem = (property, value) => {
    try {
      localStorage.setItem(property, value);
    } catch (e) {
      console.error(e);
    }
  };
  const defineSettingProperty = (property, defaultValue, storageToValue) => {
    const name = "_" + property;
    Object.defineProperty(root, property, {
      get: () => root[name],
      set: (value) => {
        root[name] = value;
        triggerSettingsChanged(property);
        setStorageItem(property, value);
      }
    });
    let stored = getStorageItem(property);
    if (stored !== null) {
      stored = storageToValue(stored);
    }
    root[name] = stored === null ? defaultValue : stored;
  };
  const fromBooleanString = (value) => {
    if (value === "true") {
      return true;
    } else if (value === "false") {
      return false;
    } else {
      return null;
    }
  };
  defineSettingProperty("enableImagePreviews", true, fromBooleanString);
  defineSettingProperty("enableFormatting", true, fromBooleanString);
  defineSettingProperty("enableUserAvatars", true, fromBooleanString);
  defineSettingProperty("enableAnimatedEmoji", true, fromBooleanString);
  return root;
}();

// scripts/processor.mjs
var filter = {
  byUser: (user) => (message) => message.u === user,
  byTime: (timeStart, timeEnd) => (message) => message.t >= timeStart && message.t <= timeEnd,
  byContents: (substr) => (message) => ("m" in message ? message.m : "").indexOf(substr) !== -1,
  byRegex: (regex) => (message) => regex.test("m" in message ? message.m : ""),
  withImages: () => (message) => message.e && message.e.some((embed) => embed.type === "image") || message.a && message.a.some(discord_default.isImageAttachment),
  withDownloads: () => (message) => message.a && message.a.some((attachment) => !discord_default.isImageAttachment(attachment)),
  withEmbeds: () => (message) => message.e && message.e.length > 0,
  withAttachments: () => (message) => message.a && message.a.length > 0,
  isEdited: () => (message) => "te" in message ? message.te : (message.f & 1) === 1
};
var sorter = {
  oldestToNewest: (key1, key2) => {
    if (key1.length === key2.length) {
      return key1 > key2 ? 1 : key1 < key2 ? -1 : 0;
    } else {
      return key1.length > key2.length ? 1 : -1;
    }
  },
  newestToOldest: (key1, key2) => {
    if (key1.length === key2.length) {
      return key1 > key2 ? -1 : key1 < key2 ? 1 : 0;
    } else {
      return key1.length > key2.length ? -1 : 1;
    }
  }
};
var processor_default = {
  FILTER: filter,
  SORTER: sorter
};

// scripts/state.mjs
var state_default = function() {
  let loadedFileMeta;
  let loadedFileData;
  let loadedMessages;
  let filterFunction;
  let selectedChannel;
  let currentPage;
  let messagesPerPage;
  const getUser = function(id) {
    return loadedFileMeta.users[id] || { "name": "&lt;unknown&gt;" };
  };
  const getUserList = function() {
    return loadedFileMeta ? loadedFileMeta.users : [];
  };
  const getServer = function(id) {
    return loadedFileMeta.servers[id] || { "name": "&lt;unknown&gt;", "type": "unknown" };
  };
  const generateChannelHierarchy = function() {
    const hierarchy = /* @__PURE__ */ new Map();
    if (!loadedFileMeta) {
      return hierarchy;
    }
    function getChildren(parentId) {
      let children = hierarchy.get(parentId);
      if (!children) {
        children = /* @__PURE__ */ new Set();
        hierarchy.set(parentId, children);
      }
      return children;
    }
    for (const [id, channel] of Object.entries(loadedFileMeta.channels)) {
      getChildren(channel.parent || "").add(id);
    }
    const unreachableIds = new Set(hierarchy.keys());
    function reachIds(parentId) {
      unreachableIds.delete(parentId);
      const children = hierarchy.get(parentId);
      if (children) {
        for (const id of children) {
          reachIds(id);
        }
      }
    }
    reachIds("");
    const rootChildren = getChildren("");
    for (const unreachableId of unreachableIds) {
      for (const id of hierarchy.get(unreachableId)) {
        rootChildren.add(id);
      }
      hierarchy.delete(unreachableId);
    }
    return hierarchy;
  };
  const generateChannelOrder = function() {
    if (!loadedFileMeta) {
      return {};
    }
    const channels = loadedFileMeta.channels;
    const hierarchy = generateChannelHierarchy();
    function getSortedSubTree(parentId) {
      const children = hierarchy.get(parentId);
      if (!children) {
        return [];
      }
      const sortedChildren = Array.from(children);
      sortedChildren.sort((id1, id2) => {
        const c1 = channels[id1];
        const c2 = channels[id2];
        const s1 = getServer(c1.server);
        const s2 = getServer(c2.server);
        return s1.type.localeCompare(s2.type, "en") || s1.name.toLocaleLowerCase().localeCompare(s2.name.toLocaleLowerCase(), void 0, { numeric: true }) || (c1.position || -1) - (c2.position || -1) || c1.name.toLocaleLowerCase().localeCompare(c2.name.toLocaleLowerCase(), void 0, { numeric: true });
      });
      const subTree = [];
      for (const id of sortedChildren) {
        subTree.push(id);
        subTree.push(...getSortedSubTree(id));
      }
      return subTree;
    }
    const orderArray = getSortedSubTree("");
    const orderMap = {};
    for (let i = 0; i < orderArray.length; i++) {
      orderMap[orderArray[i]] = i;
    }
    return orderMap;
  };
  const getChannelList = function() {
    if (!loadedFileMeta) {
      return [];
    }
    const channels = loadedFileMeta.channels;
    const channelOrder = generateChannelOrder();
    return Object.keys(channels).map((key) => ({
      "id": key,
      "name": channels[key].name,
      "server": getServer(channels[key].server),
      "msgcount": getFilteredMessageKeys(key).length,
      "topic": channels[key].topic || "",
      "nsfw": channels[key].nsfw || false
    })).sort((ac, bc) => {
      return channelOrder[ac.id] - channelOrder[bc.id];
    });
  };
  const getMessages = function(channel) {
    return loadedFileData[channel] || {};
  };
  const getMessageById = function(id) {
    for (const messages of Object.values(loadedFileData)) {
      if (id in messages) {
        return messages[id];
      }
    }
    return null;
  };
  const getMessageChannel = function(id) {
    for (const [channel, messages] of Object.entries(loadedFileData)) {
      if (id in messages) {
        return channel;
      }
    }
    return null;
  };
  const getMessageList = function() {
    if (!loadedMessages) {
      return [];
    }
    const messages = getMessages(selectedChannel);
    const startIndex = messagesPerPage * (root.getCurrentPage() - 1);
    return loadedMessages.slice(startIndex, !messagesPerPage ? void 0 : startIndex + messagesPerPage).map((key) => {
      const message = messages[key];
      const user = getUser(message.u);
      const avatar = user.avatar ? { id: message.u, path: user.avatar } : null;
      const obj = {
        user,
        avatar,
        "timestamp": message.t,
        "jump": key
      };
      if ("m" in message) {
        obj["contents"] = message.m;
      }
      if ("e" in message) {
        obj["embeds"] = message.e.map((embed) => JSON.parse(embed));
      }
      if ("a" in message) {
        obj["attachments"] = message.a;
      }
      if ("te" in message) {
        obj["edit"] = message.te;
      }
      if ("r" in message) {
        const replyMessage = getMessageById(message.r);
        const replyUser = replyMessage ? getUser(replyMessage.u) : null;
        const replyAvatar = replyUser && replyUser.avatar ? { id: replyMessage.u, path: replyUser.avatar } : null;
        obj["reply"] = replyMessage ? {
          "id": message.r,
          "user": replyUser,
          "avatar": replyAvatar,
          "contents": replyMessage.m
        } : null;
      }
      if ("re" in message) {
        obj["reactions"] = message.re;
      }
      return obj;
    });
  };
  let eventOnUsersRefreshed;
  let eventOnChannelsRefreshed;
  let eventOnMessagesRefreshed;
  const triggerUsersRefreshed = function() {
    eventOnUsersRefreshed && eventOnUsersRefreshed(getUserList());
  };
  const triggerChannelsRefreshed = function(selectedChannel2) {
    eventOnChannelsRefreshed && eventOnChannelsRefreshed(getChannelList(), selectedChannel2);
  };
  const triggerMessagesRefreshed = function() {
    eventOnMessagesRefreshed && eventOnMessagesRefreshed(getMessageList());
  };
  const getFilteredMessageKeys = function(channel) {
    const messages = getMessages(channel);
    let keys = Object.keys(messages);
    if (filterFunction) {
      keys = keys.filter((key) => filterFunction(messages[key]));
    }
    return keys;
  };
  const root = {
    onChannelsRefreshed(callback) {
      eventOnChannelsRefreshed = callback;
    },
    onMessagesRefreshed(callback) {
      eventOnMessagesRefreshed = callback;
    },
    onUsersRefreshed(callback) {
      eventOnUsersRefreshed = callback;
    },
    uploadFile(meta, data) {
      if (loadedFileMeta != null) {
        throw "A file is already loaded!";
      }
      if (typeof meta !== "object" || typeof data !== "object") {
        throw "Invalid file format!";
      }
      loadedFileMeta = meta;
      loadedFileData = data;
      loadedMessages = null;
      selectedChannel = null;
      currentPage = 1;
      triggerUsersRefreshed();
      triggerChannelsRefreshed();
      triggerMessagesRefreshed();
      settings_default.onSettingsChanged(() => triggerMessagesRefreshed());
    },
    getChannelName(channel) {
      const channelObj = loadedFileMeta.channels[channel];
      return channelObj && channelObj.name || channel;
    },
    getUserName(user) {
      const userObj = loadedFileMeta.users[user];
      return userObj && userObj.name || user;
    },
    getUserDisplayName(user) {
      const userObj = loadedFileMeta.users[user];
      return userObj && (userObj.displayName || userObj.name) || user;
    },
    selectChannel(channel) {
      currentPage = 1;
      selectedChannel = channel;
      loadedMessages = getFilteredMessageKeys(channel).sort(processor_default.SORTER.oldestToNewest);
      triggerMessagesRefreshed();
    },
    setMessagesPerPage(amount) {
      messagesPerPage = amount;
      triggerMessagesRefreshed();
    },
    updateCurrentPage(action) {
      switch (action) {
        case "first":
          currentPage = 1;
          break;
        case "prev":
          currentPage = Math.max(1, currentPage - 1);
          break;
        case "next":
          currentPage = Math.min(this.getPageCount(), currentPage + 1);
          break;
        case "last":
          currentPage = this.getPageCount();
          break;
        case "pick":
          const page = parseInt(prompt("Select page:", currentPage), 10);
          if (!page && page !== 0) {
            return;
          }
          currentPage = Math.max(1, Math.min(this.getPageCount(), page));
          break;
      }
      triggerMessagesRefreshed();
    },
    getCurrentPage() {
      const total = this.getPageCount();
      if (currentPage > total && total > 0) {
        currentPage = total;
      }
      return currentPage || 1;
    },
    getPageCount() {
      return !loadedMessages ? 0 : !messagesPerPage ? 1 : Math.ceil(loadedMessages.length / messagesPerPage);
    },
    navigateToMessage(id) {
      if (!loadedMessages) {
        return -1;
      }
      const channel = getMessageChannel(id);
      if (channel !== null && channel !== selectedChannel) {
        triggerChannelsRefreshed(channel);
        this.selectChannel(channel);
      }
      const index = loadedMessages.indexOf(id);
      if (index === -1) {
        return -1;
      }
      currentPage = Math.max(1, Math.min(this.getPageCount(), 1 + Math.floor(index / messagesPerPage)));
      triggerMessagesRefreshed();
      return index % messagesPerPage;
    },
    setActiveFilter(filter2) {
      switch (filter2 ? filter2.type : "") {
        case "user":
          filterFunction = processor_default.FILTER.byUser(filter2.value);
          break;
        case "contents":
          filterFunction = processor_default.FILTER.byContents(filter2.value);
          break;
        case "withimages":
          filterFunction = processor_default.FILTER.withImages();
          break;
        case "withdownloads":
          filterFunction = processor_default.FILTER.withDownloads();
          break;
        case "edited":
          filterFunction = processor_default.FILTER.isEdited();
          break;
        default:
          filterFunction = null;
          break;
      }
      this.hasActiveFilter = filterFunction != null;
      triggerChannelsRefreshed(selectedChannel);
      if (selectedChannel) {
        this.selectChannel(selectedChannel);
      }
    }
  };
  root.hasActiveFilter = false;
  return root;
}();

// scripts/discord.mjs
var discord_default = function() {
  const regex = {
    formatBold: /\*\*([\s\S]+?)\*\*(?!\*)/g,
    formatItalic1: /\*([\s\S]+?)\*(?!\*)/g,
    formatItalic2: /_([\s\S]+?)_(?!_)\b/g,
    formatUnderline: /__([\s\S]+?)__(?!_)/g,
    formatStrike: /~~([\s\S]+?)~~(?!~)/g,
    formatCodeInline: /(`+)\s*([\s\S]*?[^`])\s*\1(?!`)/g,
    formatCodeBlock: /```(?:([A-z0-9\-]+?)\n+)?\n*([^]+?)\n*```/g,
    formatUrl: /(\b(?:https?|ftp|file):\/\/[-A-Z0-9+&@#\/%?=~_|!:,.;]*[-A-Z0-9+&@#\/%=~_|])/ig,
    formatUrlNoEmbed: /<(\b(?:https?|ftp|file):\/\/[-A-Z0-9+&@#\/%?=~_|!:,.;]*[-A-Z0-9+&@#\/%=~_|])>/ig,
    specialEscapedBacktick: /\\`/g,
    specialEscapedSingle: /\\([*_\\])/g,
    specialEscapedDouble: /\\__|_\\_|\\_\\_|\\~~|~\\~|\\~\\~/g,
    specialUnescaped: /([*_~\\])/g,
    mentionRole: /&lt;@&(\d+?)&gt;/g,
    mentionUser: /&lt;@!?(\d+?)&gt;/g,
    mentionChannel: /&lt;#(\d+?)&gt;/g,
    customEmojiStatic: /&lt;:([^:]+):(\d+?)&gt;/g,
    customEmojiAnimated: /&lt;a:([^:]+):(\d+?)&gt;/g
  };
  let templateChannelServer;
  let templateChannelPrivate;
  let templateMessageNoAvatar;
  let templateMessageWithAvatar;
  let templateUserAvatar;
  let templateAttachmentDownload;
  let templateEmbedImage;
  let templateEmbedImageWithSize;
  let templateEmbedRich;
  let templateEmbedRichNoDescription;
  let templateEmbedUrl;
  let templateEmbedUnsupported;
  let templateReaction;
  let templateReactionCustom;
  const fileUrlProcessor = function(serverToken) {
    if (typeof serverToken === "string") {
      return (url) => "/get-downloaded-file/" + encodeURIComponent(url) + "?token=" + encodeURIComponent(serverToken);
    } else {
      return (url) => url;
    }
  }(window.DHT_SERVER_TOKEN);
  const getEmoji = function(name, id, extension) {
    const tag = ":" + name + ":";
    return "<img src='" + fileUrlProcessor("https://cdn.discordapp.com/emojis/" + id + "." + extension) + "' alt='" + tag + "' title='" + tag + "' class='emoji'>";
  };
  const processMessageContents = function(contents) {
    let processed = dom_default.escapeHTML(contents.replace(regex.formatUrlNoEmbed, "$1"));
    if (settings_default.enableFormatting) {
      const escapeHtmlMatch = (full, match) => "&#" + match.charCodeAt(0) + ";";
      processed = processed.replace(regex.specialEscapedBacktick, "&#96;").replace(regex.formatCodeBlock, (full, ignore, match) => "<code class='block'>" + match.replace(regex.specialUnescaped, escapeHtmlMatch) + "</code>").replace(regex.formatCodeInline, (full, ignore, match) => "<code class='inline'>" + match.replace(regex.specialUnescaped, escapeHtmlMatch) + "</code>").replace(regex.specialEscapedSingle, escapeHtmlMatch).replace(regex.specialEscapedDouble, (full) => full.replace(/\\/g, "").replace(/(.)/g, escapeHtmlMatch)).replace(regex.formatBold, "<b>$1</b>").replace(regex.formatUnderline, "<u>$1</u>").replace(regex.formatItalic1, "<i>$1</i>").replace(regex.formatItalic2, "<i>$1</i>").replace(regex.formatStrike, "<s>$1</s>");
    }
    const animatedEmojiExtension = settings_default.enableAnimatedEmoji ? "gif" : "webp";
    processed = processed.replace(regex.formatUrl, "<a href='$1' target='_blank' rel='noreferrer'>$1</a>").replace(regex.mentionChannel, (full, match) => "<span class='link mention-chat'>#" + state_default.getChannelName(match) + "</span>").replace(regex.mentionUser, (full, match) => "<span class='link mention-user' title='" + state_default.getUserName(match) + "'>@" + state_default.getUserDisplayName(match) + "</span>").replace(regex.customEmojiStatic, (full, m1, m2) => getEmoji(m1, m2, "webp")).replace(regex.customEmojiAnimated, (full, m1, m2) => getEmoji(m1, m2, animatedEmojiExtension));
    return "<p>" + processed + "</p>";
  };
  const getAvatarUrlObject = function(avatar) {
    return { url: fileUrlProcessor("https://cdn.discordapp.com/avatars/" + avatar.id + "/" + avatar.path + ".webp") };
  };
  const getImageEmbed = function(url, image) {
    if (!settings_default.enableImagePreviews) {
      return "";
    }
    if (image.width && image.height) {
      return templateEmbedImageWithSize.apply({ url: fileUrlProcessor(url), src: fileUrlProcessor(image.url), width: image.width, height: image.height });
    } else {
      return templateEmbedImage.apply({ url: fileUrlProcessor(url), src: fileUrlProcessor(image.url) });
    }
  };
  const isImageUrl = function(url) {
    const dot = url.pathname.lastIndexOf(".");
    const ext = dot === -1 ? "" : url.pathname.substring(dot).toLowerCase();
    return ext === ".png" || ext === ".gif" || ext === ".jpg" || ext === ".jpeg";
  };
  return {
    setup() {
      templateChannelServer = new template_default([
        "<div class='channel' data-channel='{id}'>",
        "<div class='info' title='{topic}'><strong class='name'>#{name}</strong>{nsfw}<span class='tag'>{msgcount}</span></div>",
        "<span class='server'>{server.name} ({server.type})</span>",
        "</div>"
      ].join(""));
      templateChannelPrivate = new template_default([
        "<div class='channel' data-channel='{id}'>",
        "<div class='info'><strong class='name'>{name}</strong><span class='tag'>{msgcount}</span></div>",
        "<span class='server'>({server.type})</span>",
        "</div>"
      ].join(""));
      templateMessageNoAvatar = new template_default([
        "<div>",
        "<div class='reply-message'>{reply}</div>",
        "<h2><strong class='username' title='{user.name}'>{user.displayName}</strong><span class='info time'>{timestamp}</span>{edit}{jump}</h2>",
        "<div class='message'>{contents}{embeds}{attachments}</div>",
        "{reactions}",
        "</div>"
      ].join(""));
      templateMessageWithAvatar = new template_default([
        "<div>",
        "<div class='reply-message reply-message-with-avatar'>{reply}</div>",
        "<div class='avatar-wrapper'>",
        "<div class='avatar'>{avatar}</div>",
        "<div>",
        "<h2><strong class='username' title='{user.name}'>{user.displayName}</strong><span class='info time'>{timestamp}</span>{edit}{jump}</h2>",
        "<div class='message'>{contents}{embeds}{attachments}</div>",
        "{reactions}",
        "</div>",
        "</div>",
        "</div>"
      ].join(""));
      templateUserAvatar = new template_default([
        "<img src='{url}' alt=''>"
      ].join(""));
      templateAttachmentDownload = new template_default([
        "<a href='{url}' class='embed download'>Download {name}</a>"
      ].join(""));
      templateEmbedImage = new template_default([
        "<a href='{url}' class='embed thumbnail loading'><img src='{src}' alt='' onload='window.DISCORD.handleImageLoad(this)' onerror='window.DISCORD.handleImageLoadError(this)'></a><br>"
      ].join(""));
      templateEmbedImageWithSize = new template_default([
        "<a href='{url}' class='embed thumbnail loading'><img src='{src}' width='{width}' height='{height}' alt='' onload='window.DISCORD.handleImageLoad(this)' onerror='window.DISCORD.handleImageLoadError(this)'></a><br>"
      ].join(""));
      templateEmbedRich = new template_default([
        "<div class='embed download'><a href='{url}' class='title'>{title}</a><p class='desc'>{description}</p></div>"
      ].join(""));
      templateEmbedRichNoDescription = new template_default([
        "<div class='embed download'><a href='{url}' class='title'>{title}</a></div>"
      ].join(""));
      templateEmbedUrl = new template_default([
        "<a href='{url}' class='embed download'>{url}</a>"
      ].join(""));
      templateEmbedUnsupported = new template_default([
        "<div class='embed download'><p>(Unsupported embed)</p></div>"
      ].join(""));
      templateReaction = new template_default([
        "<span class='reaction-wrapper'><span class='reaction-emoji'>{n}</span><span class='count'>{c}</span></span>"
      ].join(""));
      templateReactionCustom = new template_default([
        "<span class='reaction-wrapper'><img src='{url}' alt=':{n}:' title=':{n}:' class='reaction-emoji-custom'><span class='count'>{c}</span></span>"
      ].join(""));
    },
    handleImageLoad(ele) {
      ele.parentElement.classList.remove("loading");
    },
    handleImageLoadError(ele) {
      ele.onerror = null;
      ele.parentElement.classList.remove("loading");
      ele.setAttribute("alt", "(image attachment not found)");
    },
    isImageAttachment(attachment) {
      const url = dom_default.tryParseUrl(attachment.url);
      return url != null && isImageUrl(url);
    },
    getChannelHTML(channel) {
      return (channel.server.type === "server" ? templateChannelServer : templateChannelPrivate).apply(channel, (property, value) => {
        if (property === "nsfw") {
          return value ? "<span class='tag'>NSFW</span>" : "";
        }
      });
    },
    getMessageHTML(message) {
      return (settings_default.enableUserAvatars ? templateMessageWithAvatar : templateMessageNoAvatar).apply(message, (property, value) => {
        if (property === "avatar") {
          return value ? templateUserAvatar.apply(getAvatarUrlObject(value)) : "";
        } else if (property === "user.displayName") {
          return value ? value : message.user.name;
        } else if (property === "timestamp") {
          return dom_default.getHumanReadableTime(value);
        } else if (property === "contents") {
          return value && value.length > 0 ? processMessageContents(value) : "";
        } else if (property === "embeds") {
          if (!value) {
            return "";
          }
          return value.map((embed) => {
            if (!embed.url) {
              return templateEmbedUnsupported.apply(embed);
            } else if ("image" in embed && embed.image.url) {
              return getImageEmbed(embed.url, embed.image);
            } else if ("thumbnail" in embed && embed.thumbnail.url) {
              return getImageEmbed(embed.url, embed.thumbnail);
            } else if ("title" in embed && "description" in embed) {
              return templateEmbedRich.apply(embed);
            } else if ("title" in embed) {
              return templateEmbedRichNoDescription.apply(embed);
            } else {
              return templateEmbedUrl.apply(embed);
            }
          }).join("");
        } else if (property === "attachments") {
          if (!value) {
            return "";
          }
          return value.map((attachment) => {
            const url = fileUrlProcessor(attachment.url);
            if (!discord_default.isImageAttachment(attachment) || !settings_default.enableImagePreviews) {
              return templateAttachmentDownload.apply({ url, name: attachment.name });
            } else if ("width" in attachment && "height" in attachment) {
              return templateEmbedImageWithSize.apply({ url, src: url, width: attachment.width, height: attachment.height });
            } else {
              return templateEmbedImage.apply({ url, src: url });
            }
          }).join("");
        } else if (property === "edit") {
          return value ? "<span class='info edited'>Edited " + dom_default.getHumanReadableTime(value) + "</span>" : "";
        } else if (property === "jump") {
          return state_default.hasActiveFilter ? "<span class='info jump' data-jump='" + value + "'>Jump to message</span>" : "";
        } else if (property === "reply") {
          if (!value) {
            return value === null ? "<span class='reply-contents reply-missing'>(replies to an unknown message)</span>" : "";
          }
          const user = "<span class='reply-username' title='" + value.user.name + "'>" + (value.user.displayName ?? value.user.name) + "</span>";
          const avatar = settings_default.enableUserAvatars && value.avatar ? "<span class='reply-avatar'>" + templateUserAvatar.apply(getAvatarUrlObject(value.avatar)) + "</span>" : "";
          const contents = value.contents ? "<span class='reply-contents'>" + processMessageContents(value.contents) + "</span>" : "";
          return "<span class='jump' data-jump='" + value.id + "'>Jump to reply</span><span class='user'>" + avatar + user + "</span>" + contents;
        } else if (property === "reactions") {
          if (!value) {
            return "";
          }
          return "<div class='reactions'>" + value.map((reaction) => {
            if ("id" in reaction) {
              const ext = reaction.a && settings_default.enableAnimatedEmoji ? "gif" : "webp";
              const url = fileUrlProcessor("https://cdn.discordapp.com/emojis/" + reaction.id + "." + ext);
              return templateReactionCustom.apply({ url, n: reaction.n, c: reaction.c });
            } else {
              return templateReaction.apply(reaction);
            }
          }).join("") + "</div>";
        }
      });
    }
  };
}();

// scripts/gui.mjs
var gui_default = /* @__PURE__ */ function() {
  let eventOnOptMessagesPerPageChanged;
  let eventOnOptMessageFilterChanged;
  let eventOnNavButtonClicked;
  const getActiveFilter = function() {
    const active = dom_default.fcls("active", dom_default.id("opt-filter-list"));
    return active && active.value !== "" ? {
      "type": active.getAttribute("data-filter-type"),
      "value": active.value
    } : null;
  };
  const triggerFilterChanged = function() {
    eventOnOptMessageFilterChanged && eventOnOptMessageFilterChanged(getActiveFilter());
  };
  const showModal = function(width, html) {
    const dialog = dom_default.id("dialog");
    dialog.innerHTML = html;
    dialog.style.width = width + "px";
    dialog.style.marginLeft = -width / 2 + "px";
    dom_default.id("modal").classList.add("visible");
    return dialog;
  };
  const showSettingsModal = function() {
    showModal(560, `
<label><input id='dht-cfg-imgpreviews' type='checkbox'> Image Previews</label><br>
<label><input id='dht-cfg-formatting' type='checkbox'> Message Formatting</label><br>
<label><input id='dht-cfg-useravatars' type='checkbox'> User Avatars</label><br>
<label><input id='dht-cfg-animemoji' type='checkbox'> Animated Emoji</label><br>`);
    const setupCheckBox = function(id, settingName) {
      const ele = dom_default.id(id);
      ele.checked = settings_default[settingName];
      ele.addEventListener("change", () => settings_default[settingName] = ele.checked);
    };
    setupCheckBox("dht-cfg-imgpreviews", "enableImagePreviews");
    setupCheckBox("dht-cfg-formatting", "enableFormatting");
    setupCheckBox("dht-cfg-useravatars", "enableUserAvatars");
    setupCheckBox("dht-cfg-animemoji", "enableAnimatedEmoji");
  };
  const showInfoModal = function() {
    const linkGH = "https://github.com/chylex/Discord-History-Tracker";
    showModal(560, `
<p>Discord History Tracker is developed by <a href='https://chylex.com'>chylex</a> as an <a href='${linkGH}/blob/master/LICENSE.md'>open source</a> project.</p>
<p>Please, report any issues and suggestions to the <a href='${linkGH}/issues'>tracker</a>. If you want to support the development, please spread the word and consider <a href='https://www.patreon.com/chylex'>becoming a patron</a> or <a href='https://ko-fi.com/chylex'>buying me a coffee</a>. Any support is appreciated!</p>
<p><a href='${linkGH}/issues'>Issue Tracker</a> &nbsp;&mdash;&nbsp; <a href='${linkGH}'>GitHub Repository</a> &nbsp;&mdash;&nbsp; <a href='https://twitter.com/chylexmc'>Developer's Twitter</a></p>`);
  };
  return {
    // ---------
    // GUI setup
    // ---------
    setup() {
      const inputMessageFilter = dom_default.id("opt-messages-filter");
      const containerFilterList = dom_default.id("opt-filter-list");
      const resetActiveFilter = function() {
        inputMessageFilter.value = "";
        inputMessageFilter.dispatchEvent(new Event("change"));
        dom_default.id("opt-filter-contents").value = "";
      };
      inputMessageFilter.value = "";
      inputMessageFilter.addEventListener("change", () => {
        dom_default.cls("active", containerFilterList).forEach((ele) => ele.classList.remove("active"));
        if (inputMessageFilter.value) {
          containerFilterList.querySelector("[data-filter-type='" + inputMessageFilter.value + "']").classList.add("active");
        }
        triggerFilterChanged();
      });
      Array.prototype.forEach.call(containerFilterList.children, (ele) => {
        ele.addEventListener(ele.tagName === "SELECT" ? "change" : "input", () => triggerFilterChanged());
      });
      dom_default.id("opt-messages-per-page").addEventListener("change", () => {
        eventOnOptMessagesPerPageChanged && eventOnOptMessagesPerPageChanged();
      });
      dom_default.tag("button", dom_default.fcls("nav")).forEach((button) => {
        button.disabled = true;
        button.addEventListener("click", () => {
          eventOnNavButtonClicked && eventOnNavButtonClicked(button.getAttribute("data-nav"));
        });
      });
      dom_default.id("btn-settings").addEventListener("click", () => {
        showSettingsModal();
      });
      dom_default.id("btn-about").addEventListener("click", () => {
        showInfoModal();
      });
      dom_default.id("messages").addEventListener("click", (e) => {
        const jump = e.target.getAttribute("data-jump");
        if (jump) {
          resetActiveFilter();
          const index = state_default.navigateToMessage(jump);
          if (index === -1) {
            alert("Message not found.");
          } else {
            dom_default.id("messages").children[index].scrollIntoView();
          }
        }
      });
      dom_default.id("overlay").addEventListener("click", () => {
        dom_default.id("modal").classList.remove("visible");
        dom_default.id("dialog").innerHTML = "";
      });
    },
    // -----------------
    // Event registering
    // -----------------
    /**
     * Sets a callback for when the user changes the messages per page option. The callback is not passed any arguments.
     */
    onOptionMessagesPerPageChanged(callback) {
      eventOnOptMessagesPerPageChanged = callback;
    },
    /**
     * Sets a callback for when the user changes the active filter. The callback is passed either null or an object such as { type: <filter type>, value: <filter value> }.
     */
    onOptMessageFilterChanged(callback) {
      eventOnOptMessageFilterChanged = callback;
    },
    /**
     * Sets a callback for when the user clicks a navigation button. The callback is passed one of the following strings: first, prev, next, last.
     */
    onNavigationButtonClicked(callback) {
      eventOnNavButtonClicked = callback;
    },
    // ----------------------
    // Options and navigation
    // ----------------------
    /**
     * Returns the selected amount of messages per page.
     */
    getOptionMessagesPerPage() {
      const messagesPerPage = dom_default.id("opt-messages-per-page");
      return parseInt(messagesPerPage.value, 10);
    },
    updateNavigation(currentPage, totalPages) {
      dom_default.id("nav-page-current").innerHTML = currentPage;
      dom_default.id("nav-page-total").innerHTML = totalPages || "?";
      dom_default.id("nav-first").disabled = currentPage === 1;
      dom_default.id("nav-prev").disabled = currentPage === 1;
      dom_default.id("nav-pick").disabled = (totalPages || 0) <= 1;
      dom_default.id("nav-next").disabled = currentPage === (totalPages || 1);
      dom_default.id("nav-last").disabled = currentPage === (totalPages || 1);
    },
    // --------------
    // Updating lists
    // --------------
    /**
     * Updates the channel list and sets up their click events. The callback is triggered whenever a channel is selected, and takes the channel ID as its argument.
     */
    updateChannelList(channels, selected, callback) {
      const eleChannels = dom_default.id("channels");
      if (!channels) {
        eleChannels.innerHTML = "";
      } else {
        if (getActiveFilter() != null) {
          channels = channels.filter((channel) => channel.msgcount > 0);
        }
        eleChannels.innerHTML = channels.map((channel) => discord_default.getChannelHTML(channel)).join("");
        Array.prototype.forEach.call(eleChannels.children, (ele) => {
          ele.addEventListener("click", () => {
            const currentChannel = dom_default.fcls("active", eleChannels);
            if (currentChannel) {
              currentChannel.classList.remove("active");
            }
            ele.classList.add("active");
            callback(ele.getAttribute("data-channel"));
          });
        });
        if (selected) {
          const activeChannel = eleChannels.querySelector("[data-channel='" + selected + "']");
          activeChannel && activeChannel.classList.add("active");
        }
      }
    },
    updateMessageList(messages) {
      dom_default.id("messages").innerHTML = messages ? messages.map((message) => discord_default.getMessageHTML(message)).join("") : "";
    },
    updateUserList(users) {
      const eleSelect = dom_default.id("opt-filter-user");
      while (eleSelect.length > 1) {
        eleSelect.remove(1);
      }
      const options = [];
      for (const id of Object.keys(users)) {
        const user = users[id];
        const option = document.createElement("option");
        option.value = id;
        option.text = user.displayName ? `${user.displayName} (${user.name})` : user.name;
        options.push(option);
      }
      options.sort((a, b) => a.text.toLocaleLowerCase().localeCompare(b.text.toLocaleLowerCase()));
      options.forEach((option) => eleSelect.add(option));
    },
    scrollMessagesToTop() {
      dom_default.id("messages").scrollTop = 0;
    }
  };
}();

// scripts/polyfills.mjs
ReadableStream.prototype.values ??= function({ preventCancel = false } = {}) {
  const reader = this.getReader();
  return {
    async next() {
      try {
        const result = await reader.read();
        if (result.done) {
          reader.releaseLock();
        }
        return result;
      } catch (e) {
        reader.releaseLock();
        throw e;
      }
    },
    async return(value) {
      if (!preventCancel) {
        const cancelPromise = reader.cancel(value);
        reader.releaseLock();
        await cancelPromise;
      } else {
        reader.releaseLock();
      }
      return { done: true, value };
    },
    [Symbol.asyncIterator]() {
      return this;
    }
  };
};
ReadableStream.prototype[Symbol.asyncIterator] ??= ReadableStream.prototype.values;

// scripts/bootstrap.mjs
window.DISCORD = discord_default;
document.addEventListener("DOMContentLoaded", () => {
  discord_default.setup();
  gui_default.setup();
  gui_default.onOptionMessagesPerPageChanged(() => {
    state_default.setMessagesPerPage(gui_default.getOptionMessagesPerPage());
  });
  state_default.setMessagesPerPage(gui_default.getOptionMessagesPerPage());
  gui_default.onOptMessageFilterChanged((filter2) => {
    state_default.setActiveFilter(filter2);
  });
  gui_default.onNavigationButtonClicked((action) => {
    state_default.updateCurrentPage(action);
  });
  state_default.onUsersRefreshed((users) => {
    gui_default.updateUserList(users);
  });
  state_default.onChannelsRefreshed((channels, selected) => {
    gui_default.updateChannelList(channels, selected, state_default.selectChannel);
  });
  state_default.onMessagesRefreshed((messages) => {
    gui_default.updateNavigation(state_default.getCurrentPage(), state_default.getPageCount());
    gui_default.updateMessageList(messages);
    gui_default.scrollMessagesToTop();
  });
  async function fetchUrl(path, contentType) {
    const response = await fetch("/" + path + "?token=" + encodeURIComponent(window.DHT_SERVER_TOKEN) + "&session=" + encodeURIComponent(window.DHT_SERVER_SESSION), {
      method: "GET",
      headers: {
        "Content-Type": contentType
      },
      credentials: "omit",
      redirect: "error"
    });
    if (!response.ok) {
      throw "Unexpected response status: " + response.statusText;
    }
    return response;
  }
  async function processLines(response, callback) {
    let body = "";
    for await (const chunk of response.body.pipeThrough(new TextDecoderStream("utf-8"))) {
      body += chunk;
      let startIndex = 0;
      while (true) {
        const endIndex = body.indexOf("\n", startIndex);
        if (endIndex === -1) {
          break;
        }
        callback(body.substring(startIndex, endIndex));
        startIndex = endIndex + 1;
      }
      body = body.substring(startIndex);
    }
    if (body !== "") {
      callback(body);
    }
  }
  async function loadData() {
    try {
      // Parse metadata JSON
      const metadataTag = document.getElementById("viewer-metadata");
      const metadataJson = JSON.parse(metadataTag.textContent);

      // Parse messages NDJSON
      const messagesTag = document.getElementById("viewer-messages");
      const rawNdjson = messagesTag.textContent.trim().split('\n');
      const messages = {};

      for (const line of rawNdjson) {
        if (!line.trim()) continue;
        const message = JSON.parse(line);
        const channel = message.c;
        const channelMessages = messages[channel] || (messages[channel] = {});
        channelMessages[message.id] = message;

        delete message.id;
        delete message.c;
      }

      state_default.uploadFile(metadataJson, messages);
    } catch (e) {
      console.error(e);
      alert("Could not load data, see console for details.");
      document.querySelector("#channels > div.loading").remove();
    }
  }
  loadData();
});
"""

if __name__ == '__main__':
    main()
