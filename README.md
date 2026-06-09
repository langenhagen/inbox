# Inbox
Quick and simple email organizing tool.

Fetches emails from your accounts, asks LLM to cluster them by sender address and subject and writes
the emails as HTML files in fitting folders.

Then, you are free to rename the folders, move the emails to other folders, delete them or do
whatever. Subsequent runs will pick up nicely.  
You may as well create folders before running the tool.

Uses the [opencode](https://opencode.ai/) CLI tool to access an LLM, potentially for free access to
an LLM without signin.

## Setup

Export a JSON array of accounts as `EMAIL_ACCOUNTS` before running:
```bash
export EMAIL_ACCOUNTS='[
  {"server": "imap.mail.com", "port": 993, "user": "foo@mail.com", "password": "s3cret"},
  {"server": "imap.domain.com", "port": 993, "user": "bar@domain.com", "password": "hunter2"}
]'
```

Keep the secrets outside of the project and not in an env/shell where an agent might read them.

E.g., store this in a file, e.g. `~/.secrets/` and source them when into the shell in which you run
the tool:
```fish
source ~/.secrets/inbox.env
uv run inbox.py
```

## Usage

```bash
uv run inbox.py
uv run inbox.py --output-dir ~/sorted-mail
MAILS_DIR=~/archive-mail uv run inbox.py
uv run inbox.py --help
```
Emails are sorted as HTML files under `~/inbox/<folder>/` by default.  
Pass `--output-dir` (or set `MAILS_DIR`) to choose a different root.  
The file `seen.txt` in that directory tracks already-processed messages for dedup.


## License
See [LICENSE](LICENSE) file.
