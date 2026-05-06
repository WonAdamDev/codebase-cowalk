"""codebase-cowalk-tail.

Plugin monitor entry point. Tails the global events.jsonl and prints each new
line to stdout. Claude Code's plugin Monitor mechanism delivers each stdout line
as a notification to the Claude session, so user actions in the HTML page
(re-explanation requests, comments, status toggles) propagate back to Claude.

The script is platform-independent (no `tail` binary required) and starts at the
end of file so existing history is not replayed on every startup.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .paths import events_path


def main() -> None:
    p: Path = events_path()
    # start at end of file
    try:
        with p.open("r", encoding="utf-8") as f:
            f.seek(0, 2)  # SEEK_END
            offset = f.tell()
    except FileNotFoundError:
        offset = 0
        p.touch()

    while True:
        try:
            with p.open("r", encoding="utf-8") as f:
                f.seek(offset)
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line if line.endswith("\n") else line + "\n")
                        sys.stdout.flush()
                        offset = f.tell()
                    else:
                        time.sleep(0.5)
                        # detect truncation
                        new_size = p.stat().st_size if p.exists() else 0
                        if new_size < offset:
                            offset = 0
                            break
        except (FileNotFoundError, OSError):
            time.sleep(1.0)
            offset = 0


if __name__ == "__main__":
    main()
