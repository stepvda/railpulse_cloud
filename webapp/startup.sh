#!/usr/bin/env bash
#
# App Service startup command for the Streamlit dashboard.
#
# App Service Linux's Python image otherwise looks for a WSGI callable and starts
# gunicorn. Streamlit is its own long-running server, so the platform needs an
# explicit startup command:
#
#   az webapp config set --startup-file "bash /home/site/wwwroot/startup.sh"
#
# TWO THINGS THAT MAKE THE OBVIOUS ONE-LINER FAIL WITH EXIT 127
# The first version of this was a single `python -m streamlit run app.py`, and the
# container died with "exit code 127" — shell for *command not found* — after
# 16 seconds, with App Service then reverting by stopping the whole site (which
# takes Kudu down too, so the logs become unreachable at exactly the moment you
# want them).
#
#   1. The image provides `python3`. It does not necessarily provide `python`,
#      so `python -m streamlit` is not found.
#   2. Oryx installs the dependencies into a virtualenv named `antenv`, and a
#      CUSTOM startup command does not run with it activated. Without it,
#      streamlit is not importable even once the interpreter is found.
#
# Hence: activate antenv when it exists, then resolve the interpreter rather than
# assuming its name. The diagnostics printed below cost nothing and go to the
# container log, so the next failure of this kind is one log read away instead of
# a bisect.
#
set -euo pipefail

cd /home/site/wwwroot

if [[ -f antenv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source antenv/bin/activate
  echo "startup: activated the antenv virtualenv"
else
  echo "startup: no antenv/bin/activate — relying on the image's site-packages"
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "startup: FATAL — no python3 or python on PATH (PATH=$PATH)" >&2
  exit 1
fi

echo "startup: interpreter $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
echo "startup: streamlit $("$PYTHON_BIN" -m streamlit version 2>&1 || echo 'NOT IMPORTABLE')"
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
