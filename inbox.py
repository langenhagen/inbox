#!/usr/bin/env python3
"""Fetch and sort IMAP emails into folders via opencode classification."""

import argparse
import html
import imaplib
import json
import os
import re
from email import message_from_bytes, utils
from email.message import Message
from pathlib import Path

from opencode_client import ask

_BOLD = "\033[1m"
_RESET = "\033[0m"

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _get_text_body(msg: Message) -> str:
    """Return the plain text body of a message."""
    if msg.get_content_type() == "text/plain":
        charset = msg.get_content_charset() or "utf-8"
        try:
            payload = msg.get_payload(decode=True)
            return payload.decode(charset, errors="replace")
        except (UnicodeDecodeError, LookupError):
            return ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                try:
                    payload = part.get_payload(decode=True)
                    return payload.decode(charset, errors="replace")
                except (UnicodeDecodeError, LookupError):
                    return ""
    return ""


def _format_date(date_str: str) -> str:
    """Parse email Date header and return YYYY-MM-DD-HH-MM-SS in local time."""
    m = re.search(
        r"(\d{1,2})\s+(\w{3})\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})",
        date_str,
    )
    if not m:
        return "unknown-date"
    day, mon, year, hour, minute, second = m.groups()
    month = _MONTHS.get(mon, 1)
    return f"{year}-{month:02d}-{int(day):02d}-{int(hour):02d}-{minute}-{second}"


def _slugify(text: str) -> str:
    """Replace special chars with hyphens, collapse runs."""
    text = re.sub(r"[^\w\s.-]", "", text)
    text = re.sub(r"[\s.]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _seen_id(msg: Message) -> str:
    """Build a unique id for dedup from Date, sender email, and Subject."""
    date = _format_date(re.sub(r"\s+", " ", (msg["Date"] or "")).strip())
    sender = utils.parseaddr(msg["From"] or "")[1]
    subject = re.sub(r"\s+", " ", (msg["Subject"] or "")).strip()
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


def _email_to_html(date_str: str, sender: str, subject: str, body: str) -> str:
    """Render email as a minimal HTML document."""
    esc = html.escape
    from_line = f"<strong>From:</strong> {esc(sender)}"
    date_line = f"<strong>Date:</strong> {esc(date_str)}"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(subject)}</title></head>
<body>
<h1>{esc(subject)}</h1>
<p>{from_line}<br>{date_line}</p>
<hr>
<pre>{esc(body)}</pre>
</body></html>"""


def _classify_email(sender: str, subject: str, folders: list[str]) -> str:
    """Use opencode to determine the target folder for an email."""
    folder_list = ", ".join(folders) if folders else "(none)"
    prompt = (
        f"Classify this email into one of the existing folders. "
        f"Reply with ONLY a single folder name, no other text.\n\n"
        f"The sender email address is the primary signal; "
        f"the subject is secondary context.\n\n"
        f"Sender: {sender}\n"
        f"Subject: {subject}\n"
        f"Existing folders: {folder_list}\n\n"
        f"If none fit, suggest a new short name (lowercase, hyphens for spaces) "
        f"based on the company or topic. Example: fastalyze, github-notifications."
    )
    name = ask(prompt)
    name = name.lower().strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w-]", "", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-") or "misc"


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _write_email(  # noqa: PLR0913  # all 6 args needed
    mails_dir: Path,
    folder: str,
    date_str: str,
    sender: str,
    subject: str,
    body: str,
) -> None:
    """Write an email as an HTML file into the classified folder."""
    folder_path = mails_dir / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    sender_slug = utils.parseaddr(sender)[1]
    subject_slug = _slugify(subject)
    filename = f"{date_str}-{sender_slug}--{subject_slug}.html"
    filepath = folder_path / filename
    html_content = _email_to_html(date_str, sender, subject, body)
    filepath.write_text(html_content, encoding="utf-8")


def _process_message(
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
    sender = msg["From"] or "(unknown sender)"
    subject = msg["Subject"] or "(no subject)"
    date_str = _format_date(date_raw)
    line = f"{date_str[:10]} {date_str[11:].replace('-', ':')}"
    print(f"{line} {sender} :: {subject}")  # noqa: T201  # CLI output
    body = _get_text_body(msg)
    folders = _list_folders(mails_dir)
    folder = _classify_email(sender, subject, folders)
    print(f"  folder: {_BOLD}{folder}{_RESET}")  # noqa: T201  # CLI output
    touched_folders.add(folder)

    _write_email(mails_dir, folder, date_str, sender, subject, body)
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
            _process_message(msg, seen_ids, mails_dir, seen_path, touched_folders)
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

    if touched_folders:
        print(f"\n{'=' * 40}")  # noqa: T201  # CLI output
        print("Run complete. New mails in:\n")  # noqa: T201  # CLI output
        for f in sorted(touched_folders):
            print(f"  {_BOLD}{f}{_RESET}")  # noqa: T201  # CLI output
        print()  # noqa: T201  # CLI output
    else:
        print("Run complete.\n")  # noqa: T201  # CLI output


if __name__ == "__main__":
    main()
