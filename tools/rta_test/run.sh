#!/bin/sh
# Build both sample formats of the RTA kernel natively and run the acceptance
# tests.  test_rta.py compiles the shared libraries itself, so this is just a
# stable entry point with a numpy check in front of it.
set -e
cd "$(dirname "$0")"
python3 -c "import numpy" 2>/dev/null || {
    echo "error: numpy is required (pip3 install numpy)" >&2
    exit 1
}
python3 test_rta.py "$@"
exec python3 test_engine.py
