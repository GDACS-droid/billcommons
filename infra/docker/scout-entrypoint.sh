#!/bin/sh
set -eu

# Check only local service dependencies before accepting work.  The Python
# check intentionally prints no secret values and never opens a browser.
python -m billcommons_scout check
exec "$@"
