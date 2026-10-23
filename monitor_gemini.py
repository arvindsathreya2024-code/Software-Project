"""
Nginx log monitor + Gemini (Google AI Studio) error analysis.

Watches logs/access.log and logs/error.log. When it sees errors
(5xx responses or [error]/[crit]/[alert]/[emerg] lines), it waits a few
seconds to collect them, sends them to Gemini, and saves a solution report
in the "reports" folder.

Setup:
    pip install google-genai
    PowerShell:  $env:GEMINI_API_KEY="your-key-here"
Run:
    python monitor_gemini.py
    python monitor_gemini.py --list-models   (shows which models your key can use)
"""

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import errors

# ---------- Settings ----------
LOG_DIR = Path("logs")
ACCESS_LOG = LOG_DIR / "access.log"
ERROR_LOG = LOG_DIR / "error.log"
CONFIG_FILE = Path("nginx.conf")      # sent to Gemini as extra context (optional)
REPORT_DIR = Path("reports")

# If you get a "model not found" error, run:  python monitor_gemini.py --list-models
# and paste one of the Flash / Flash-Lite names here.
MODEL = "gemini-3.5-flash-lite"

POLL_SECONDS = 1          # how often to check the logs
BATCH_WAIT_SECONDS = 10   # collect errors for this long before sending
MAX_LINES_PER_REPORT = 50 # safety limit per request

ERROR_LEVELS = ("[error]", "[crit]", "[alert]", "[emerg]")
STATUS_PATTERN = re.compile(r'"\s(\d{3})\s')  # matches: " 500 


# ---------- Log tailing ----------
class LogTail:
    """Reads only the new lines added to a file since the last check."""

    def __init__(self, path: Path):
        self.path = path
        self.pos = None  # None = not started yet

    def read_new_lines(self):
        if not self.path.exists():
            return []

        size = self.path.stat().st_size

        if self.pos is None:      # first time: start from the end (ignore old lines)
            self.pos = size
            return []
        if size < self.pos:       # file was truncated or rotated: start over
            self.pos = 0
        if size == self.pos:
            return []

        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()

        last_newline = data.rfind(b"\n")
        if last_newline == -1:    # incomplete line, wait for the rest
            return []

        self.pos += last_newline + 1
        text = data[: last_newline + 1].decode("utf-8", errors="replace")
        return text.splitlines()


def is_error_line(source: str, line: str) -> bool:
    """Decide whether a log line counts as an error."""
    if source == "error.log":
        return any(level in line for level in ERROR_LEVELS)

    match = STATUS_PATTERN.search(line)   # access.log
    return bool(match) and int(match.group(1)) >= 500


# ---------- Gemini analysis ----------
def build_prompt(error_list):
    config_text = ""
    if CONFIG_FILE.exists():
        config_text = CONFIG_FILE.read_text(encoding="utf-8", errors="replace")

    error_block = "\n".join(f"[{src}] {line}" for src, line in error_list)

    return f"""You are a DevOps expert. Nginx is running in a Docker container.
The log monitor detected the errors below.

## Detected errors
{error_block}

## Current nginx.conf
{config_text or "(not available)"}

Write a short solution report in Markdown with these sections:
1. **Summary**: what happened, in plain language
2. **Root cause**: the most likely reason (say if the error looks intentional,
   for example an endpoint configured to return 500)
3. **Solution**: numbered steps to fix it, with config or commands where useful
4. **Prevention**: how to avoid it in future
5. **Severity**: Low / Medium / High, with a one-line reason
"""


def analyze_with_gemini(client, error_list, retries=2):
    prompt = build_prompt(error_list)
    for attempt in range(retries + 1):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt)
            return response.text or "(Gemini returned an empty response)"
        except errors.APIError as e:
            # 429 = free-tier rate limit hit: wait and try again
            if e.code == 429 and attempt < retries:
                print("Rate limit reached, waiting 30 seconds before retrying...")
                time.sleep(30)
                continue
            raise


def save_report(report_text, error_list):
    REPORT_DIR.mkdir(exist_ok=True)
    name = datetime.now().strftime("report_%Y%m%d_%H%M%S.md")
    path = REPORT_DIR / name

    header = f"# Nginx Error Report ({datetime.now():%Y-%m-%d %H:%M:%S})\n\n"
    header += f"Errors analysed: {len(error_list)}\n\n---\n\n"
    path.write_text(header + report_text, encoding="utf-8")
    return path


def explain_api_error(e):
    """Print a friendly hint for common problems."""
    print(f"Gemini API error {e.code}: {e.message}")
    if e.code in (400, 401, 403):
        print("Hint: the API key looks wrong. Create a key in Google AI Studio "
              "and set it again with  $env:GEMINI_API_KEY=\"your-key\"")
    elif e.code == 404:
        print("Hint: the model name is not available. Run  "
              "python monitor_gemini.py --list-models  and update MODEL.")
    elif e.code == 429:
        print("Hint: free-tier limit reached. Wait a minute and try again.")


# ---------- Main loop ----------
def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY is not set.")
        print('PowerShell:  $env:GEMINI_API_KEY="your-key-here"')
        return

    client = genai.Client(api_key=api_key)

    if "--list-models" in sys.argv:
        print("Models available to your key:")
        try:
            for m in client.models.list():
                print(" ", m.name)
        except errors.APIError as e:
            explain_api_error(e)
        return

    LOG_DIR.mkdir(exist_ok=True)

    tails = {
        "access.log": LogTail(ACCESS_LOG),
        "error.log": LogTail(ERROR_LOG),
    }

    pending = []          # errors waiting to be sent
    first_error_time = None

    print(f"Monitoring {LOG_DIR.resolve()} using {MODEL} ... (Ctrl+C to stop)")

    try:
        while True:
            for source, tail in tails.items():
                for line in tail.read_new_lines():
                    if is_error_line(source, line):
                        print(f"[detected] {source}: {line}")
                        pending.append((source, line))
                        if first_error_time is None:
                            first_error_time = time.time()

            # Send the batch once the wait time has passed
            if pending and time.time() - first_error_time >= BATCH_WAIT_SECONDS:
                batch = pending[:MAX_LINES_PER_REPORT]
                print(f"\nSending {len(batch)} error(s) to Gemini...")
                try:
                    report = analyze_with_gemini(client, batch)
                    path = save_report(report, batch)
                    print("\n" + "=" * 60)
                    print(report)
                    print("=" * 60)
                    print(f"Report saved to: {path}\n")
                except errors.APIError as e:
                    explain_api_error(e)

                pending = pending[MAX_LINES_PER_REPORT:]
                first_error_time = time.time() if pending else None

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
