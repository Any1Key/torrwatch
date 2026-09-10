#!/bin/sh
set -eu

# A bind mount can be created by Docker as root. Restrict this ownership change
# to the sole project state directory before dropping privileges permanently.
chown -R torrwatch:torrwatch /data
exec gosu torrwatch "$@"
