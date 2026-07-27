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

# How many containers `make wait` expects to go healthy. Named so the two places that count
# them cannot disagree.
SERVICE_COUNT := 5

.DEFAULT_GOAL := help
.PHONY: help env images up up-metrics down restart wait nuke logs ps psql topics lint

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template if it does not exist
	@test -f deploy/compose/.env \
		|| { cp deploy/compose/.env.example deploy/compose/.env; echo "created deploy/compose/.env"; }

images: ## Build every service image (each service builds its own)
	$(MAKE) -C ../wallet-service docker
	$(MAKE) -C ../payment-service docker
	$(MAKE) -C ../catalog-service docker
	$(MAKE) -C ../order-service docker
	$(MAKE) -C ../media-service docker

up: env ## Start Postgres, Redis, Kafka and all five services
	$(COMPOSE) up -d
	@echo
	@echo "  wallet   REST http://localhost:8080   gRPC localhost:9090"
	@echo "  payment  REST http://localhost:8081   gRPC localhost:9091"
	@echo "  catalog  REST http://localhost:8082   docs /docs"
	@echo "  order    REST http://localhost:8083   docs /docs"
	@echo "  media    REST http://localhost:8084   docs /docs"
	@echo
	@echo "  make wait   # block until every service is healthy"

up-metrics: env ## Start the platform plus Prometheus and Grafana
	$(COMPOSE) --profile metrics up -d
	@echo "  grafana http://localhost:3001   prometheus http://localhost:9095"

down: ## Stop everything, keeping data
	$(COMPOSE) --profile metrics down

restart: ## Restart the services, after rebuilding an image
	$(COMPOSE) up -d --force-recreate \
		wallet-service payment-service catalog-service order-service media-service

wait: ## Block until every service reports ready
	@echo "waiting for all $(SERVICE_COUNT) services to become healthy..."
	@for i in $$(seq 1 90); do \
		up=$$($(COMPOSE) ps --format '{{.Service}} {{.Health}}' \
			| awk '$$1 ~ /-service$$/ && $$2 == "healthy"' | wc -l | tr -d ' '); \
		[ "$$up" = "$(SERVICE_COUNT)" ] && { echo "all $(SERVICE_COUNT) healthy"; exit 0; }; \
		sleep 2; \
	done; \
	echo "timed out with $$up/$(SERVICE_COUNT) healthy; try 'make ps' and 'make logs'"; exit 1

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

lint: ## Validate the compose file and the config files it mounts
	@$(COMPOSE) config -q && echo "compose is valid"
	@python3 -c "import glob,json,yaml; [ (json.load(open(p)) if p.endswith('.json') else list(yaml.safe_load_all(open(p)))) for p in glob.glob('deploy/**/*.y*ml', recursive=True) + glob.glob('deploy/**/*.json', recursive=True) ]" \
		&& echo "all mounted config files parse"
