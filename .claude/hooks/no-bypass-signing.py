#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from no_bypass_signing_impl import evaluate


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not cmd:
        sys.exit(0)
    blocked, message = evaluate(cmd)
    if blocked:
        sys.stderr.write(message)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
