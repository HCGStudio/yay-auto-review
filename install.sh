#!/bin/sh
set -eu
cd -- "$(CDPATH='' dirname -- "$0")"
exec python3 -m yay_auto_review install "$@"
