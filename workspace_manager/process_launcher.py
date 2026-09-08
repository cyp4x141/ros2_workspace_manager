"""Standard-library-only Linux launcher used by the QProcess adapter."""

import json
import os
from pathlib import Path
import sys


def group_is_alive(pgid):
    """Ignore zombies, which cannot execute or write build artifacts."""
    uncertain = False
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and int(fields[3]) == pgid and fields[0] != 'Z':
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            uncertain = True
    if uncertain:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            pass
    return False


def main():
    if len(sys.argv) < 3:
        sys.stderr.write('Expected a task token and command.\n')
        return 2
    token, program, *arguments = sys.argv[1:]

    def report(data):
        sys.stderr.write('\x1eWM:' + token + ':' + json.dumps(data) + '\n')
        sys.stderr.flush()

    try:
        os.setsid()
        report({'event': 'ready', 'pgid': os.getpgrp()})
        os.execvpe(program, [program, *arguments], os.environ)
    except (OSError, ValueError) as exc:
        report({'event': 'launch_error', 'message': str(exc)})
        return 127


if __name__ == '__main__':
    sys.exit(main())
