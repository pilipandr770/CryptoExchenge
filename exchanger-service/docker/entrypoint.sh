#!/bin/sh
set -e

flask db upgrade

# worker-class gthread (not the default sync) -- sync workers block on
# the raw socket recv() while waiting for the next request on a kept-alive
# connection, and that blocking call can't send the arbiter's heartbeat,
# so an idle keep-alive connection alone (no slow request involved)
# eventually trips the --timeout watchdog as a false-positive "WORKER
# TIMEOUT" and gets killed/respawned. gthread's threaded request handling
# doesn't have this failure mode.
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --worker-class gthread --timeout 25 wsgi:app
