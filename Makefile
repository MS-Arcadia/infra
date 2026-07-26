# Arcadia platform tasks.
#
#   make images    build both service images (delegates to each service repo)
#   make up        start the platform
#   make down      stop it, keeping data
#   make nuke      stop it and delete the volumes
#
# This repository compiles nothing. `images` calls each service's own Makefile, because
# how a service is built is that service's business.

COMPOSE := docker compose --project-directory deploy/compose -f deploy/compose/docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help env images up up-metrics down restart nuke logs ps psql topics lint

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template if it does not exist
	@test -f deploy/compose/.env \
		|| { cp deploy/compose/.env.example deploy/compose/.env; echo "created deploy/compose/.env"; }

images: ## Build both service images (each service builds its own)
	$(MAKE) -C ../wallet-service docker
	$(MAKE) -C ../payment-service docker

up: env ## Start Postgres, Redis, Kafka and both services
	$(COMPOSE) up -d
	@echo
	@echo "  wallet   REST http://localhost:8080   gRPC localhost:9090"
	@echo "  payment  REST http://localhost:8081   gRPC localhost:9091"
	@echo
	@echo "  curl -s localhost:8080/readyz"

up-metrics: env ## Start the platform plus Prometheus and Grafana
	$(COMPOSE) --profile metrics up -d
	@echo "  grafana http://localhost:3001   prometheus http://localhost:9095"

down: ## Stop everything, keeping data
	$(COMPOSE) --profile metrics down

restart: ## Restart just the two services, after rebuilding an image
	$(COMPOSE) up -d --force-recreate wallet-service payment-service

nuke: ## Stop everything and delete the volumes
	$(COMPOSE) --profile metrics down -v
	@echo "volumes removed; the next 'make up' re-runs the database init script"

logs: ## Follow logs from every container
	$(COMPOSE) logs -f --tail=100

ps: ## Show container status
	$(COMPOSE) ps

psql: ## Open psql against the wallet database
	$(COMPOSE) exec postgres psql -U wallet_user -d arcadia_wallet

topics: ## List Kafka topics
	$(COMPOSE) exec kafka kafka-topics.sh --bootstrap-server localhost:9092 --list

lint: ## Validate the compose file and the Kubernetes manifests
	@$(COMPOSE) config -q && echo "compose is valid"
	@for f in deploy/k8s/*.yaml; do \
		python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" "$$f" \
			&& echo "$$f parses"; \
	done
