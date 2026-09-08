import sys

from .cli import main

status = main()
if status != 0:
    # A failed buffered stdout write must not be retried at shutdown, which
    # would otherwise emit another exception and override our exit status.
    try:
        sys.stdout.close()
    except OSError:
        pass
raise SystemExit(status)
