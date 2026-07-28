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
SERVICE_COUNT := 6

# MinIO, for the migration target. The root credentials come from .env like everything else.
MC_IMAGE := minio/mc:RELEASE.2025-04-16T18-13-26Z
MINIO_BUCKET ?= arcadia-media
COMPOSE_NETWORK := arcadia_default
MINIO_ROOT_USER := $(shell grep -E "^MINIO_ROOT_USER=" deploy/compose/.env 2>/dev/null | cut -d= -f2-)
MINIO_ROOT_PASSWORD := $(shell grep -E "^MINIO_ROOT_PASSWORD=" deploy/compose/.env 2>/dev/null | cut -d= -f2-)

.DEFAULT_GOAL := help
.PHONY: help env images up up-metrics down restart wait nuke logs ps psql topics lint e2e e2e-health e2e-install

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template if it does not exist
	@test -f deploy/compose/.env \
		|| { cp deploy/compose/.env.example deploy/compose/.env; echo "created deploy/compose/.env"; }
	@# .env is never overwritten — it holds local edits. But a template that has moved on is
	@# worth saying out loud: a variable added to .env.example is simply absent from .env, and
	@# the service that needs it fails at boot with nothing pointing at the stale file.
	@missing=""; \
	for key in $$(grep -oE '^[A-Z_][A-Z0-9_]*' deploy/compose/.env.example); do \
		grep -qE "^$$key=" deploy/compose/.env || missing="$$missing $$key"; \
	done; \
	if [ -n "$$missing" ]; then \
		echo; \
		echo "  NOTE: deploy/compose/.env is missing variables that .env.example defines:"; \
		for key in $$missing; do echo "    $$key"; done; \
		echo; \
		echo "  Add them, or start over with:  rm deploy/compose/.env && make env"; \
		echo; \
	fi

images: ## Build every service image (each service builds its own)
	$(MAKE) -C ../auth-profile-service docker
	$(MAKE) -C ../wallet-service docker
	$(MAKE) -C ../payment-service docker
	$(MAKE) -C ../catalog-service docker
	$(MAKE) -C ../order-service docker
	$(MAKE) -C ../media-service docker

up: env ## Start Postgres, Redis, Kafka, MinIO and all six services
	$(COMPOSE) up -d
	@echo
	@echo "  auth     REST http://localhost:8085   docs /docs"
	@echo "  wallet   REST http://localhost:8080   gRPC localhost:9090"
	@echo "  payment  REST http://localhost:8081   gRPC localhost:9091"
	@echo "  catalog  REST http://localhost:8082   docs /docs"
	@echo "  order    REST http://localhost:8083   docs /docs"
	@echo "  media    REST http://localhost:8084   docs /docs"
	@echo
	@echo "  minio    S3   http://localhost:9000   console http://localhost:9001"
	@echo "           log in with MINIO_ROOT_USER / MINIO_ROOT_PASSWORD from .env"
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

media-migrate: ## Copy files from the media-data volume into the MinIO bucket
	@# For switching STORAGE_BACKEND from filesystem to s3 on a stack that already has files.
	@# Without this the metadata rows survive and their bytes do not: every download 404s, and
	@# `make e2e` says so — `test_no_media_row_lacks_its_bytes` is the check that catches it.
	@#
	@# `mc mirror` rather than `cp`: it skips what is already there, so running this twice is
	@# safe and interrupting it is recoverable. The sharded layout is preserved as-is, because
	@# the object key in the database is exactly that path.
	@echo "mirroring the media-data volume into the $(MINIO_BUCKET) bucket"
	@docker run --rm --network $(COMPOSE_NETWORK) 		-v arcadia_media-data:/from:ro 		-e MC_HOST_target="http://$(MINIO_ROOT_USER):$(MINIO_ROOT_PASSWORD)@minio:9000" 		--entrypoint /bin/sh $(MC_IMAGE) -c 		'mc mirror --overwrite --exclude ".tmp/*" --exclude ".readyz" /from target/$(MINIO_BUCKET)'
	@echo
	@echo "done. 'make e2e' will confirm every metadata row has its bytes."

topics: ## List Kafka topics
	$(COMPOSE) exec kafka kafka-topics.sh --bootstrap-server localhost:9092 --list

e2e: ## Run the end-to-end tests against the running platform (see test/e2e/README.md)
	@# Not part of CI, and not part of `make up`. It needs the whole platform running and it
	@# changes state — publishing a game, moving money — so it is something you ask for.
	@test -d test/e2e/.venv || { \
		echo "creating test/e2e/.venv"; \
		python3 -m venv test/e2e/.venv; \
		test/e2e/.venv/bin/pip install --quiet -r test/e2e/requirements.txt; \
	}
	@cd test/e2e && .venv/bin/python -m pytest . -q

e2e-health: ## Just the platform invariants: DLQs empty, outboxes drained, ledger balances
	@test -d test/e2e/.venv || $(MAKE) e2e-install
	@cd test/e2e && .venv/bin/python -m pytest test_99_platform_health.py -v

e2e-install: ## Create the virtualenv the end-to-end tests use
	python3 -m venv test/e2e/.venv
	test/e2e/.venv/bin/pip install --quiet -r test/e2e/requirements.txt
	@echo "installed"

lint: ## Validate the compose file and the config files it mounts
	@$(COMPOSE) config -q && echo "compose is valid"
	@python3 -c "import glob,json,yaml; [ (json.load(open(p)) if p.endswith('.json') else list(yaml.safe_load_all(open(p)))) for p in glob.glob('deploy/**/*.y*ml', recursive=True) + glob.glob('deploy/**/*.json', recursive=True) ]" \
		&& echo "all mounted config files parse"
