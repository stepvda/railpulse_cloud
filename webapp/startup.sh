#!/usr/bin/env bash
#
# App Service startup command for the Streamlit dashboard.
#
# App Service Linux's Python image otherwise looks for a WSGI callable and starts
# gunicorn. Streamlit is its own long-running server, so the platform needs an
# explicit startup command:
#
#   az webapp config set --startup-file "bash startup.sh"      <- RELATIVE
#
# WHY THE STARTUP COMMAND MUST BE RELATIVE — the bug that cost two days
# An ABSOLUTE startup command (`bash /home/site/wwwroot/startup.sh`) fails with
# exit 127 — shell for *command not found* — because that file is not there.
# With an Oryx build, /home/site/wwwroot contains only:
#
#     output.tar.zst      the compressed build output (160 MB here)
#     oryx-manifest.toml
#     requirements.txt
#
# No app.py, no startup.sh, no antenv. The platform extracts that archive to a
# temp directory at container start, activates the virtualenv inside it, and runs
# the startup command from THERE. So the command must be relative.
#
# The tell was that none of the `startup:` lines below ever appeared in the
# container log: the script was never reached at all. Two earlier theories —
# that the image lacks `python` (it has python3), and that a custom command does
# not inherit Oryx's activated antenv — were plausible, testable, and both wrong.
# The interpreter probe below is kept anyway: it is cheap insurance and it prints
# what it found, so a future failure is one log read away instead of a bisect.
#
set -euo pipefail

# NO `cd /home/site/wwwroot`. With Oryx, wwwroot holds only the COMPRESSED build
# output (`output.tar.zst`, 160 MB here) plus a manifest — no app.py, no
# startup.sh, no antenv. The platform extracts that archive to a temp directory
# at container start, activates the virtualenv inside it, and runs the startup
# command from there. An absolute startup command like
#   bash /home/site/wwwroot/startup.sh
# therefore names a path that never exists, the container exits 127 before a
# single line of this script runs (which is why no diagnostics appeared in the
# log), and on the Free plan the restart loop then burns the daily CPU quota.
# The startup command must be RELATIVE: `bash startup.sh`.
echo "startup: cwd $(pwd)"

if [[ -f antenv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source antenv/bin/activate
  echo "startup: activated /home/site/wwwroot/antenv"
else
  echo "startup: no antenv/bin/activate here — will search for an interpreter"
fi

# Pick the FIRST interpreter that can actually import streamlit, rather than the
# first one that exists. Which of these is correct depends on where Oryx put the
# virtualenv and on whether the platform's init script activated it before
# running this command — and getting that wrong yields a container that exits
# during startup, which on the F1 Free plan burns the daily CPU quota and then
# takes Kudu down with it, so the log explaining the failure is unreachable.
# Probing is cheap; a wrong guess costs a day.
PYTHON_BIN=""
for candidate in \
    /home/site/wwwroot/antenv/bin/python \
    /home/site/wwwroot/antenv/bin/python3 \
    "$(command -v python3 || true)" \
    "$(command -v python || true)" \
    /usr/local/bin/python3 \
    /usr/bin/python3 \
    /tmp/*/antenv/bin/python
do
  [[ -n "$candidate" && -x "$candidate" ]] || continue
  if "$candidate" -c "import streamlit" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    echo "startup: using $candidate (imports streamlit)"
    break
  fi
  echo "startup: $candidate exists but cannot import streamlit"
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "startup: FATAL — no interpreter on this container can import streamlit." >&2
  echo "startup: PATH=$PATH" >&2
  echo "startup: wwwroot contents:" >&2
  ls -la /home/site/wwwroot >&2 || true
  echo "startup: any antenv?" >&2
  ls -d /home/site/wwwroot/antenv /tmp/*/antenv 2>/dev/null >&2 || echo "  none found" >&2
  exit 1
fi

echo "startup: $("$PYTHON_BIN" --version 2>&1)"
echo "startup: binding 0.0.0.0:${PORT:-8000}"

# The platform routes inbound requests to $PORT (8000 on Linux App Service).
# Binding 0.0.0.0 rather than localhost is required: the request arrives from the
# platform's front end, not from inside the container.
exec "$PYTHON_BIN" -m streamlit run app.py \
  --server.port "${PORT:-8000}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false \
  --server.enableCORS false \
  --server.enableXsrfProtection false
#
# WHY CORS AND XSRF PROTECTION ARE OFF
# Streamlit's browser session runs over a WebSocket. Behind App Service's front
# end the request's Origin does not match what Streamlit believes its own host to
# be, so with these enabled the socket is rejected and the page loads to a
# permanent "Please wait…". Both protections guard against a hostile page driving
# a user's session — and what that would buy an attacker here is a READ-ONLY
# dashboard: data.query refuses anything that is not a SELECT, and the single
# state-changing control (the ingest button) is a server-side call carrying a
# function key the browser never sees.
#
# The honest fix is App Service authentication (Entra sign-in), which removes the
# anonymous session there is currently nothing to hijack *of*. Noted as the next
# step in docs/webapp.md.
