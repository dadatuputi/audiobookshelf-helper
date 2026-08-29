#!/usr/bin/env python3
"""Native messaging host entry point.

Deliberately thin. All the logic lives in the `absh` package, which the CLI and
the TUI use too - so what the extension does and what `absh` does on the command
line are the same code path, not two implementations that drift.

The browser launches this by absolute path with an arbitrary working directory,
and it runs from two different layouts: inside native/ in a checkout, and
beside the package in the released archive. Try both.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent, HERE):        # checkout, then unpacked archive
    if (candidate / "absh" / "host.py").is_file():
        sys.path.insert(0, str(candidate))
        break
else:  # pragma: no cover - only reachable from a broken install
    sys.exit("cannot find the absh package next to " + str(HERE))

from absh.host import main  # noqa: E402  (path resolved immediately above)

if __name__ == "__main__":
    main()
