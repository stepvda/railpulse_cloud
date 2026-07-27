# ===========================================================================
# RailPulse Cloud
# ===========================================================================
# The whole project, from nothing to a running pipeline:
#
#     az login                # once, with the @becode.education account
#     make provision          # create every Azure resource (cost settings baked in)
#     make deploy             # publish the function code
#     make smoke              # migrate, seed, ingest twice, verify
#
# Day to day:
#     make test               # offline unit tests — no Azure needed
#     make stats              # what is in the warehouse
#     make pause              # stop the compute on Friday, keep the data
# ===========================================================================

PYTHON      ?= .venv/bin/python
PIP         ?= .venv/bin/pip
SECRET_FILE ?= .azure-railpulse.env

# Reading the deployment target out of the file provision.sh wrote. `-` prefixed
# so the Makefile still parses before provisioning has ever run.
FUNCTION_APP   := $(shell [ -f $(SECRET_FILE) ] && grep -E '^FUNCTION_APP=' $(SECRET_FILE) | cut -d= -f2-)
RESOURCE_GROUP := $(shell [ -f $(SECRET_FILE) ] && grep -E '^RESOURCE_GROUP=' $(SECRET_FILE) | cut -d= -f2-)

.DEFAULT_GOAL := help
.PHONY: help venv test lint provision deploy smoke redeploy \
        migrate seed ingest stats health logs pause teardown \
        local-migrate local-ingest local-verify query clean \
        provision-web deploy-web web webapp-local webapp-logs web-url

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1;36m%-16s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Local development
# ---------------------------------------------------------------------------
venv:  ## Create .venv and install the dev requirements
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements-dev.txt
	@echo "  .venv ready"

test:  ## Run the offline test suite (no Azure, no database)
	$(PYTHON) -m pytest tests/ -q

test-v:  ## Run the tests verbosely
	$(PYTHON) -m pytest tests/ -v

lint:  ## Byte-compile every module — catches syntax errors before a 3-minute deploy
	$(PYTHON) -m compileall -q function_app/railpulse function_app/function_app.py scripts
	@echo "  all modules compile"

# ---------------------------------------------------------------------------
# Azure lifecycle
# ---------------------------------------------------------------------------
provision:  ## Create the resource group, serverless SQL, storage and Function App
	./azure/provision.sh

deploy:  ## Package function_app/ + sql/ and publish it
	./azure/deploy.sh

smoke:  ## End-to-end check of the live deployment (includes the idempotency test)
	./azure/smoke_test.sh

redeploy: lint test deploy  ## Compile, test, then publish

# ---------------------------------------------------------------------------
# Operating the deployed pipeline. The function key is fetched per call rather
# than stored anywhere, so nothing here holds a secret.
# ---------------------------------------------------------------------------
KEY = $(shell az functionapp keys list -n $(FUNCTION_APP) -g $(RESOURCE_GROUP) --query functionKeys.default -o tsv)
# `az functionapp show` returns an EMPTY defaultHostName for a Flex Consumption
# app while `az webapp show` — same ARM resource, different command module —
# returns it. Without the fallback every target below would build "https:///api/..."
# and fail with a DNS error that looks like a network problem and is not one.
URL = https://$(shell az webapp show -n $(FUNCTION_APP) -g $(RESOURCE_GROUP) --query defaultHostName -o tsv)

migrate:  ## Apply sql/*.sql through the deployed app (idempotent)
	@curl -fsS -X POST -H "x-functions-key: $(KEY)" "$(URL)/api/migrate" | python3 -m json.tool

seed:  ## Load iRail's full station catalogue
	@curl -fsS -X POST -H "x-functions-key: $(KEY)" "$(URL)/api/seed-stations" | python3 -m json.tool

ingest:  ## Poll every configured hub now
	@curl -fsS -X POST -H "x-functions-key: $(KEY)" "$(URL)/api/ingest?hubs=all" | python3 -m json.tool

stats:  ## Row counts, data quality and the hub leaderboard
	@curl -fsS -H "x-functions-key: $(KEY)" "$(URL)/api/stats" | python3 -m json.tool

health:  ## Per-station freshness
	@curl -sS -H "x-functions-key: $(KEY)" "$(URL)/api/health" | python3 -m json.tool

logs:  ## Tail the Function App logs
	az functionapp log tail -n $(FUNCTION_APP) -g $(RESOURCE_GROUP)

url:  ## Print the base URL and a ready-to-paste keyed ingest URL
	@echo "$(URL)"
	@echo "$(URL)/api/ingest?hubs=all&code=$(KEY)"

# ---------------------------------------------------------------------------
# The web dashboard (Streamlit on App Service, F1 Free tier)
# ---------------------------------------------------------------------------
WEBAPP := $(shell [ -f $(SECRET_FILE) ] && grep -E '^WEBAPP=' $(SECRET_FILE) | cut -d= -f2-)
WEB_URL = https://$(shell az webapp show -n $(WEBAPP) -g $(RESOURCE_GROUP) --query defaultHostName -o tsv 2>/dev/null)

provision-web:  ## Create the App Service plan + web app (F1 Free; SKU=B1 for Basic)
	./azure/provision_webapp.sh

deploy-web:  ## Package webapp/ and publish it
	./azure/deploy_webapp.sh

web: provision-web deploy-web  ## Provision and deploy the dashboard in one go

web-url:  ## Print the dashboard URL
	@echo "$(WEB_URL)"

webapp-logs:  ## Tail the dashboard's container log
	az webapp log tail -n $(WEBAPP) -g $(RESOURCE_GROUP)

webapp-local:  ## Run the dashboard on this machine against Azure SQL
	set -a; . ./$(SECRET_FILE); set +a; \
	  FUNCTION_APP_URL="https://$$(az webapp show -n $(FUNCTION_APP) -g $(RESOURCE_GROUP) --query defaultHostName -o tsv)" \
	  FUNCTION_KEY="$$(az functionapp keys list -n $(FUNCTION_APP) -g $(RESOURCE_GROUP) --query functionKeys.default -o tsv)" \
	  $(PYTHON) -m streamlit run webapp/app.py

# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------
pause:  ## Stop the Function App and let the database pause (keeps all data)
	./azure/teardown.sh pause

resume:  ## Start the Function App and the web app again
	az functionapp start -n $(FUNCTION_APP) -g $(RESOURCE_GROUP)
	-az webapp start -n $(WEBAPP) -g $(RESOURCE_GROUP) 2>/dev/null
	@echo "  started; the database resumes on its first query"

teardown:  ## DELETE the resource group and everything in it (irreversible)
	./azure/teardown.sh delete

cost:  ## Month-to-date spend for the resource group
	az consumption usage list --start-date $$(date -u -v1d '+%Y-%m-%d' 2>/dev/null || date -u -d "$$(date +%Y-%m-01)" '+%Y-%m-%d') \
	  --end-date $$(date -u '+%Y-%m-%d') --query "[?contains(instanceName, 'railpulse')].{resource:instanceName, cost:pretaxCost, currency:currency}" -o table \
	  2>/dev/null || echo "  Consumption API unavailable for this offer — use the portal: Cost Management + Billing"

# ---------------------------------------------------------------------------
# Running the pipeline from this machine. Needs the ODBC driver locally; see
# the docstring of scripts/local_cli.py.
# ---------------------------------------------------------------------------
local-migrate:  ## Apply the schema from this machine
	set -a; . ./$(SECRET_FILE); set +a; $(PYTHON) scripts/local_cli.py migrate

local-ingest:  ## Poll every hub from this machine
	set -a; . ./$(SECRET_FILE); set +a; $(PYTHON) scripts/local_cli.py ingest --hubs

local-verify:  ## Print counts, quality and freshness from this machine
	set -a; . ./$(SECRET_FILE); set +a; $(PYTHON) scripts/local_cli.py verify

query:  ## Run an analysis file: make query FILE=sql/analysis/a1_peak_hour.sql
	set -a; . ./$(SECRET_FILE); set +a; \
	  $(PYTHON) scripts/local_cli.py query $(FILE) --csv output/

clean:  ## Remove caches and build artefacts
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache function_app/sql *.zip
	@echo "  cleaned"
