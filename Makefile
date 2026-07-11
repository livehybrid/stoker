# Stoker developer entry points. `make help` lists targets.
.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON     ?= python3
VENV       ?= .venv
VENV_PY    := $(VENV)/bin/python
IMAGE      ?= stoker-worker:dev
SINK_PORT  ?= 18088
HEC_TOKEN  ?= dev-token
RATE       ?= 100
DURATION_S ?= 20

.PHONY: help venv test sink run-standalone docker-build docker-smoke

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create $(VENV) with worker runtime + test dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	@if [ -f worker/requirements.txt ]; then \
		$(VENV_PY) -m pip install -r worker/requirements.txt; \
	else \
		echo "worker/requirements.txt not present yet; installing agent runtime deps only"; \
		$(VENV_PY) -m pip install requests prometheus_client; \
	fi
	$(VENV_PY) -m pip install pytest pytest-timeout

test: ## Run unit tests (worker + tools) in $(VENV)
	@if [ -d worker/tests ]; then \
		$(VENV_PY) -m pytest worker/tests -q; \
	else \
		echo "worker/tests not present yet; skipping agent tests"; \
	fi
	$(VENV_PY) -m pytest tools/tests -q --timeout=60

sink: ## Run the HEC sink on SINK_PORT (foreground, Ctrl-C to stop)
	$(PYTHON) tools/hec_sink.py --port $(SINK_PORT) --token $(HEC_TOKEN) --verbose

run-standalone: ## Run the agent standalone vs the sink (start `make sink` in another terminal first)
	STOKER_STANDALONE=1 \
	STOKER_BUNDLE=packs/flatline \
	STOKER_HEC_URL=http://127.0.0.1:$(SINK_PORT) \
	STOKER_HEC_TOKEN=$(HEC_TOKEN) \
	STOKER_INDEX=main \
	STOKER_RATE_MODE=eps \
	STOKER_RATE_VALUE=$(RATE) \
	STOKER_DURATION_S=$(DURATION_S) \
	STOKER_METRICS_PORT=0 \
	PYTHONPATH=worker:worker/engines/eventgen \
	$(VENV_PY) -m stoker_agent

docker-build: ## Build the worker image locally (single arch, loaded into docker)
	docker buildx build --load -f worker/Dockerfile -t $(IMAGE) .

docker-smoke: docker-build ## Build then smoke the image: $(DURATION_S)s at $(RATE) EPS vs the sink, +/- 10 percent
	RATE=$(RATE) DURATION_S=$(DURATION_S) TOLERANCE_PCT=10 \
	SINK_PORT=$(SINK_PORT) HEC_TOKEN=$(HEC_TOKEN) \
	tools/smoke.sh docker $(IMAGE)
