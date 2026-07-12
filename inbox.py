#!/usr/bin/env python3
"""Fetch and sort IMAP emails into folders via opencode classification."""

import argparse
import datetime as dt
import html
import imaplib
import json
import os
import re
import sys
from contextlib import suppress
from email import message_from_bytes, utils
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import cast

from opencode_client import ask

_BOLD = "\033[1m"
_RESET = "\033[0m"
_RENDER_HTML = True
_BODY_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.DOTALL | re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)


def _decode_payload(msg: Message, charset: str) -> str:
    """Decode a text/plain payload, raising RuntimeError on failure."""
    try:
        payload = cast("bytes", msg.get_payload(decode=True))
        return payload.decode(charset, errors="replace")
    except (UnicodeDecodeError, LookupError) as err:
        err_msg = "Cannot parse message payload!"
        raise RuntimeError(err_msg) from err


def _decode_mime_header(raw: str) -> str:
    """Decode RFC 2047 encoded-word headers (e.g. =?UTF-8?Q?=E2=82=AC?=)."""
    return str(make_header(decode_header(raw)))


def _get_text_body(msg: Message) -> str:
    """Return the plain text body of a message."""
    if msg.get_content_type() == "text/plain":
        return _decode_payload(msg, msg.get_content_charset() or "utf-8")
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return _decode_payload(part, part.get_content_charset() or "utf-8")
    return ""


def _get_html_body(msg: Message) -> str | None:
    """Return the HTML body of a message, or None if not found."""
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(
                msg.get_content_charset() or "utf-8",
                errors="replace",
            )
        return None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace",
                    )
                return None
    return None


def _extract_html_body(html_: str) -> str:
    """Return content inside <body> tags, or the whole string if not found."""
    m = _BODY_RE.search(html_)
    return m[1] if m else html_


def _strip_scripts(html_: str) -> str:
    """Remove <script>...</script> blocks from HTML for security reasons."""
    return _SCRIPT_RE.sub("", html_)


def _parse_date(date_str: str) -> dt.datetime | None:
    """Parse email Date header and return a timezone-aware datetime."""
    clean = re.sub(r"\s+", " ", date_str).strip()
    try:
        return utils.parsedate_to_datetime(clean)
    except (TypeError, ValueError):
        print(  # noqa: T201  # CLI warning, not logging
            f"warning: could not parse date: {date_str!r}",
            file=sys.stderr,
        )
        return None


def _slugify(text: str) -> str:
    """Replace special chars with hyphens, collapse runs."""
    text = re.sub(r"[^\w\s.-]", "", text)
    text = re.sub(r"[\s.]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _seen_id(msg: Message) -> str:
    """Build a unique id for dedup from Date, sender email, and Subject."""
    raw = re.sub(r"\s+", " ", (msg["Date"] or "")).strip()
    email_date = _parse_date(raw)
    date = email_date.strftime("%Y-%m-%d-%H-%M-%S") if email_date else "unknown-date"
    sender = utils.parseaddr(msg["From"] or "")[1]
    subject = re.sub(r"\s+", " ", _decode_mime_header(msg["Subject"] or "")).strip()
    return f"{date}|||{sender}|||{subject}"


def _load_seen(path: Path) -> set[str]:
    """Load seen email ids from a text file."""
    if not path.exists():
        return set()
    return set(path.read_text(encoding="utf-8").splitlines())


def _mark_seen(path: Path, seen_id: str) -> None:
    """Append a seen email id to the file."""
    with path.open("a", encoding="utf-8") as f:
        f.write(seen_id + "\n")


def _list_folders(mails_dir: Path) -> list[str]:
    """Return sorted list of subdirectory names under mails_dir."""
    if not mails_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in mails_dir.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _load_extra_context(mails_dir: Path) -> str:
    """Return extra-context.md content if it exists, else empty string."""
    path = mails_dir / "extra-context.md"
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _email_to_html(  # noqa: PLR0913  # too-many-arguments
    email_date: dt.datetime | None,
    sender: str,
    receiver: str,
    subject: str,
    body: str,
    *,
    body_is_html: bool,
) -> str:
    """Render email as a minimal HTML document."""
    esc = html.escape
    from_line = f"<strong>From:</strong> {esc(sender)}"
    to_line = f"<strong>To:</strong> {esc(receiver)}"
    display_date = (
        email_date.strftime("%Y-%m-%d %H:%M:%S") if email_date else "unknown-date"
    )
    date_line = f"<strong>Date:</strong> {esc(display_date)}"
    body_block = body if body_is_html else f"<pre>{esc(body)}</pre>"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(subject)}</title>
<style>pre{{white-space:pre-wrap;overflow-wrap:break-word}}</style></head>
<body>
<h1>{esc(subject)}</h1>
<p>
{from_line}<br>
{to_line}<br>
{date_line}
</p>
<hr>
{body_block}
</body></html>"""


def _classify_email(
    sender: str,
    subject: str,
    folders: list[str],
    extra_context: str = "",
) -> str:
    """Use opencode to determine the target folder for an email."""
    folder_list = ", ".join(folders) if folders else "(none)"
    prompt = (
        f"Classify this email into one of the existing folders. "
        f"Reply with ONLY a single folder name, no other text.\n\n"
        f"The sender email address is the primary signal; "
        f"the subject is secondary context.\n\n"
        f"Sender: {sender}\n"
        f"Subject: {subject}\n"
        f"Existing folders: {folder_list}\n"
    )
    if extra_context:
        prompt += (
            f"\nExtra context (apply only parts if they are relevant "
            f"to this specific email):\n{extra_context}\n"
        )
    prompt += (
        "\nIf no existing folder fits, suggest a new short name "
        "(lowercase, hyphens for spaces) based on the company or topic. "
        "Example: fastalyze, github-notifications."
    )
    name = ask(prompt)
    name = name.lower().strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w-]", "", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-") or "misc"


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _save_email(  # noqa: PLR0913  # too-many-arguments
    mails_dir: Path,
    folder: str,
    email_date: dt.datetime | None,
    sender: str,
    receiver: str,
    subject: str,
    body: str,
    *,
    body_is_html: bool,
) -> None:
    """Write an email as an HTML file into the classified folder."""
    folder_path = mails_dir / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    sender_slug = utils.parseaddr(sender)[1]
    subject_slug = _slugify(subject)
    date_part = (
        email_date.strftime("%Y-%m-%d-%H-%M-%S") if email_date else "unknown-date"
    )
    filename = f"{date_part}-{sender_slug}--{subject_slug}.html"
    filepath = folder_path / filename
    html_content = _email_to_html(
        email_date,
        sender,
        receiver,
        subject,
        body,
        body_is_html=body_is_html,
    )
    filepath.write_text(html_content, encoding="utf-8")


# pylint: disable=too-many-locals
def _process_email(
    msg: Message,
    seen_ids: set[str],
    mails_dir: Path,
    seen_path: Path,
    touched_folders: set[str],
) -> None:
    """Classify and save one email if not already seen."""
    mail_id = _seen_id(msg)
    if mail_id in seen_ids:
        return

    date_raw = msg["Date"] or ""
    sender = _decode_mime_header(msg["From"] or "(unknown sender)")
    receiver = _decode_mime_header(msg["To"] or "(unknown receiver)")
    subject = _decode_mime_header(msg["Subject"] or "(no subject)")
    email_date = _parse_date(date_raw)
    line = email_date.strftime("%Y-%m-%d %H:%M:%S") if email_date else "unknown-date"
    print(f"{line} {sender} :: {subject}")  # noqa: T201  # CLI output
    if _RENDER_HTML and (html_raw := _get_html_body(msg)):
        body = _strip_scripts(_extract_html_body(html_raw))
        body_is_html = True
    else:
        body = _get_text_body(msg)
        body_is_html = False
    folders = _list_folders(mails_dir)
    extra_context = _load_extra_context(mails_dir)
    folder = _classify_email(sender, subject, folders, extra_context)
    print(f"  folder: {_BOLD}{folder}{_RESET}")  # noqa: T201  # CLI output
    touched_folders.add(folder)

    _save_email(
        mails_dir,
        folder,
        email_date,
        sender,
        receiver,
        subject,
        body,
        body_is_html=body_is_html,
    )
    _mark_seen(seen_path, mail_id)


def _process_account(
    acc: dict,
    seen_ids: set[str],
    mails_dir: Path,
    seen_path: Path,
    touched_folders: set[str],
) -> None:
    """Connect to an IMAP account and classify all inbox messages."""
    print(f"\n=== {acc['user']} ===")  # noqa: T201  # CLI output
    conn = imaplib.IMAP4_SSL(acc["server"], acc["port"])
    conn.login(acc["user"], acc["password"])
    conn.select("INBOX")
    _, msg_ids = conn.search(None, "ALL")
    for mid in (msg_ids[0] if msg_ids else b"").split():
        _, data = conn.fetch(mid.decode(), "(BODY.PEEK[])")
        raw = data[0][1] if data and data[0] else b""
        if isinstance(raw, bytes):
            msg = message_from_bytes(raw)
            _process_email(msg, seen_ids, mails_dir, seen_path, touched_folders)
    conn.logout()


def main() -> None:
    """Classify and save all inbox messages."""
    parser = argparse.ArgumentParser(
        description="Classify and sort IMAP emails into topic folders",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=os.environ.get("MAILS_DIR", "~/inbox"),
        help="Output directory (default: ~/inbox, env: MAILS_DIR)",
    )
    args = parser.parse_args()
    mails_dir = Path(args.output_dir).expanduser()
    mails_dir.mkdir(parents=True, exist_ok=True)
    seen_path = mails_dir / "seen.txt"
    seen_ids = _load_seen(seen_path)
    print(f"loaded {len(seen_ids)} seen ids")  # noqa: T201  # CLI output

    try:
        accounts = json.loads(os.environ["EMAIL_ACCOUNTS"])
    except KeyError:
        parser.error("EMAIL_ACCOUNTS environment variable is required")
    touched_folders: set[str] = set()
    for acc in accounts:
        _process_account(acc, seen_ids, mails_dir, seen_path, touched_folders)

    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if touched_folders:
        print(f"\n{'=' * 40}")  # noqa: T201  # CLI output
        print(f"Run completed at {ts}. New mails in:\n")  # noqa: T201  # CLI output
        for f in sorted(touched_folders):
            print(f"  {_BOLD}{f}{_RESET}")  # noqa: T201  # CLI output
        print()  # noqa: T201  # CLI output
    else:
        print(f"Run completed at {ts}. No new mails.\n")  # noqa: T201  # CLI output


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
