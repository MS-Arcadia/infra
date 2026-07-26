# Arcadia platform tasks.
#
# `make help` lists everything. The common paths are:
#
#   make up          start the data layer and both services
#   make up-full     the above plus the observability stack and Kafka UI
#   make logs        follow everything
#   make down        stop, keeping data
#   make nuke        stop and delete the volumes

COMPOSE_DIR  := deploy/compose
COMPOSE      := docker compose --project-directory $(COMPOSE_DIR) -f $(COMPOSE_DIR)/docker-compose.yml
K8S_DIR      := deploy/k8s
CONTRACTS    := contracts
PLATFORM     := platform

# The module cache is prepended as a file:// proxy because the default GOPROXY on some
# machines is an artefact mirror that intermittently 502s.
GOPROXY_CHAIN := file://$(shell go env GOMODCACHE)/cache/download,$(shell go env GOPROXY)
GO            := GOFLAGS=-mod=mod GOPROXY="$(GOPROXY_CHAIN)" go

.DEFAULT_GOAL := help
.PHONY: help env up up-full up-data down restart nuke logs ps psql redis-cli topics \
        platform-test platform-lint proto-lint proto-gen proto-breaking \
        k8s-build k8s-diff compose-lint lint ci

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template if it does not exist
	@if [ ! -f $(COMPOSE_DIR)/.env ]; then \
		cp $(COMPOSE_DIR)/.env.example $(COMPOSE_DIR)/.env; \
		echo "created $(COMPOSE_DIR)/.env — review the secrets section before exposing anything"; \
	else \
		echo "$(COMPOSE_DIR)/.env already exists; leaving it alone"; \
	fi

up: env ## Start Postgres, Redis, Kafka and both services
	$(COMPOSE) up -d --build
	@echo
	@echo "  wallet   REST http://localhost:8080   gRPC localhost:9090"
	@echo "  payment  REST http://localhost:8081   gRPC localhost:9091"
	@echo
	@echo "  readiness: curl -s localhost:8080/readyz | jq"

up-full: env ## Start everything, including observability and the Kafka UI
	$(COMPOSE) --profile observability --profile tools up -d --build
	@echo
	@echo "  grafana    http://localhost:3001   (anonymous admin)"
	@echo "  prometheus http://localhost:9095"
	@echo "  kafka UI   http://localhost:8085"

up-data: env ## Start only the data layer, for running a service from your IDE
	$(COMPOSE) up -d postgres redis kafka

down: ## Stop everything, keeping the data volumes
	$(COMPOSE) --profile observability --profile tools down

restart: ## Rebuild and restart just the application services
	$(COMPOSE) up -d --build --force-recreate wallet-service payment-service

nuke: ## Stop everything and delete the volumes (irreversible)
	$(COMPOSE) --profile observability --profile tools down -v
	@echo "volumes removed; the next 'make up' re-runs the database init script"

logs: ## Follow logs from every container
	$(COMPOSE) logs -f --tail=100

logs-wallet: ## Follow the wallet service only
	$(COMPOSE) logs -f --tail=200 wallet-service

logs-payment: ## Follow the payment service only
	$(COMPOSE) logs -f --tail=200 payment-service

ps: ## Show container status
	$(COMPOSE) ps

psql: ## Open psql against the wallet database
	$(COMPOSE) exec postgres psql -U wallet_user -d arcadia_wallet

psql-payment: ## Open psql against the payment database
	$(COMPOSE) exec postgres psql -U payment_user -d arcadia_payment

redis-cli: ## Open redis-cli
	$(COMPOSE) exec redis redis-cli

topics: ## List Kafka topics and their partition counts
	$(COMPOSE) exec kafka kafka-topics.sh --bootstrap-server localhost:9092 --describe

consumer-groups: ## Show consumer groups and their lag
	$(COMPOSE) exec kafka kafka-consumer-groups.sh --bootstrap-server localhost:9092 --all-groups --describe

tail-wallet-events: ## Print wallet-events as they are published
	$(COMPOSE) exec kafka kafka-console-consumer.sh \
		--bootstrap-server localhost:9092 --topic wallet-events --from-beginning

dlq: ## Show anything sitting in a dead-letter topic (should be empty)
	@for topic in payment-events.dlq user-events.dlq wallet-commands.dlq trade-events.dlq; do \
		echo "--- $$topic"; \
		$(COMPOSE) exec -T kafka kafka-run-class.sh kafka.tools.GetOffsetShell \
			--bootstrap-server localhost:9092 --topic $$topic 2>/dev/null || echo "  (topic absent)"; \
	done

# --- Shared platform module ------------------------------------------------

platform-test: ## Run the shared platform module's tests
	cd $(PLATFORM) && $(GO) test -race -count=1 ./...

platform-lint: ## Vet the shared platform module
	cd $(PLATFORM) && $(GO) vet ./...

# --- Contracts -------------------------------------------------------------

proto-lint: ## Lint the protobuf contracts
	cd $(CONTRACTS) && buf lint

proto-gen: ## Regenerate the Go code from the protobuf contracts
	cd $(CONTRACTS) && buf generate
	@echo "generated into $(PLATFORM)/gen"

proto-breaking: ## Check the contracts for breaking changes against main
	cd $(CONTRACTS) && buf breaking --against '../../.git#branch=main,subdir=contracts'

# --- Deployment manifests --------------------------------------------------

compose-lint: ## Validate the compose file
	$(COMPOSE) config -q && echo "compose file is valid"

k8s-build: ## Render both kustomize overlays
	@echo "--- namespaces"; kubectl kustomize $(K8S_DIR)/namespaces >/dev/null && echo OK
	@echo "--- staging";    kubectl kustomize $(K8S_DIR)/overlays/staging >/dev/null && echo OK
	@echo "--- prod";       kubectl kustomize $(K8S_DIR)/overlays/prod >/dev/null && echo OK

k8s-apply-staging: ## Apply the staging overlay to the current kube context
	@echo "current context: $$(kubectl config current-context)"
	@read -p "apply staging there? [y/N] " confirm && [ "$$confirm" = "y" ]
	kubectl apply -k $(K8S_DIR)/namespaces
	kubectl apply -k $(K8S_DIR)/overlays/staging

lint: compose-lint k8s-build proto-lint platform-lint ## Lint everything in this repository

ci: lint platform-test ## Everything the pipeline runs
	@echo "infra checks passed"
