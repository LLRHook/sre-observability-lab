.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
UV := uv run
PROM_IMAGE := prom/prometheus:v3.15.1
AM_IMAGE := prom/alertmanager:v0.34.1
APPROVER ?= $(USER)
ACTION ?= 0

-include .env
export

.PHONY: help up down build ps logs restart test lint fmt unit rules rules-check dashboards dashboards-check \
        promtool amtool yamllint compose-config gameday approve reject incidents noise-report \
        chaos-errors chaos-latency chaos-db chaos-reset targets stats clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- stack
up: .env ## Build and start the whole stack
	$(COMPOSE) up -d --build
	@echo "Grafana http://localhost:3000  Prometheus http://localhost:9090  Alertmanager http://localhost:9093"
	@echo "API http://localhost:8000/docs  Copilot http://localhost:8001/docs  OpenSearch http://localhost:9200"

down: ## Stop the stack (volumes kept)
	$(COMPOSE) down

build: ## Build images only
	$(COMPOSE) build

ps: ## Container status
	$(COMPOSE) ps

logs: ## Tail logs (SERVICE=name to filter)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

restart: ## Restart one service: make restart SERVICE=prometheus
	$(COMPOSE) restart $(SERVICE)

.env:
	cp .env.example .env
	@echo "created .env from .env.example"

# ---------------------------------------------------------------- quality
test: lint unit rules-check dashboards-check promtool amtool compose-config ## Everything CI runs

lint: ## ruff + yamllint
	$(UV) ruff check .
	$(UV) ruff format --check .
	$(UV) yamllint -s .

fmt: ## Format Python
	$(UV) ruff format .
	$(UV) ruff check --fix .

unit: ## pytest
	$(UV) pytest -q

rules: ## Regenerate SLO rules from slo/slos.yaml
	$(UV) python tools/slo_gen.py

rules-check: ## Fail if generated rules are stale
	$(UV) python tools/slo_gen.py --check

dashboards: ## Regenerate Grafana dashboards JSON
	$(UV) python tools/gen_dashboards.py

dashboards-check: ## Fail if dashboards JSON is stale
	$(UV) python tools/gen_dashboards.py --check

promtool: ## promtool check + unit tests (inside the pinned Prometheus image)
	docker run --rm -v "$(CURDIR)/prometheus:/p:ro" --entrypoint promtool $(PROM_IMAGE) check config /p/prometheus.yml
	docker run --rm -v "$(CURDIR)/prometheus:/p:ro" --entrypoint promtool $(PROM_IMAGE) check rules /p/rules/generated/slo_rules.yml /p/rules/static/platform.yml
	docker run --rm -v "$(CURDIR)/prometheus:/p:ro" -w /p/tests --entrypoint promtool $(PROM_IMAGE) test rules slo_alerts.test.yml platform_alerts.test.yml

amtool: ## amtool check-config + routing assertions
	ALERTMANAGER_IMAGE=$(AM_IMAGE) ./alertmanager/tests/routes_test.sh

yamllint:
	$(UV) yamllint -s .

compose-config: .env ## Validate docker-compose.yml
	$(COMPOSE) config --quiet && echo "compose config ok"

# ---------------------------------------------------------------- operations
gameday: ## Run the scripted incident; waits for `make approve` unless AUTO=1
	$(UV) python tools/gameday.py --approver "$(APPROVER)" $(if $(AUTO),--auto-approve,)

approve: ## Approve a proposed action: make approve INCIDENT=inc-... ACTION=0 APPROVER="Name"
	$(UV) python tools/copilotctl.py approve $(INCIDENT) --approver "$(APPROVER)" --action $(ACTION) $(if $(DRY_RUN),--dry-run,)

reject: ## Reject proposals: make reject INCIDENT=inc-... REASON="..."
	$(UV) python tools/copilotctl.py reject $(INCIDENT) --approver "$(APPROVER)" --reason "$(REASON)"

incidents: ## List copilot incidents
	$(UV) python tools/copilotctl.py list

noise-report: ## Alert noise report over WINDOW (default 24h)
	$(UV) python tools/alert_noise_report.py --window $(or $(WINDOW),24h)

chaos-errors: ## Inject 50% errors (RATE=0.5)
	curl -sf -X POST localhost:8000/admin/chaos -H "x-admin-token: $(ADMIN_TOKEN)" -H 'content-type: application/json' -d '{"error_rate": $(or $(RATE),0.5)}'; echo

chaos-latency: ## Inject latency (MS=800)
	curl -sf -X POST localhost:8000/admin/chaos -H "x-admin-token: $(ADMIN_TOKEN)" -H 'content-type: application/json' -d '{"latency_ms": $(or $(MS),800)}'; echo

chaos-db: ## Inject database faults
	curl -sf -X POST localhost:8000/admin/chaos -H "x-admin-token: $(ADMIN_TOKEN)" -H 'content-type: application/json' -d '{"db_fault": true}'; echo

chaos-reset: ## Clear all injected faults
	curl -sf -X POST localhost:8000/admin/chaos/reset -H "x-admin-token: $(ADMIN_TOKEN)"; echo

targets: ## Prometheus target health
	@curl -s localhost:9090/api/v1/targets | $(UV) python -c "import sys,json; [print(f\"{t['health']:<5} {t['labels']['job']:<16} {t['scrapeUrl']}\") for t in json.load(sys.stdin)['data']['activeTargets']]"

stats: ## Memory footprint
	docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}" $$($(COMPOSE) ps -q)

clean: ## Stop and delete volumes and local data
	$(COMPOSE) down -v
	rm -rf data/copilot
