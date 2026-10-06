APP ?= snap-bot
ENV ?= staging
TAG ?=

.PHONY: help bootstrap lint test build images smoke secrets ci deploy rollback clean

help: ## Show available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

bootstrap: ## Install the locked dev environment and git hooks
	scripts/bootstrap.sh

lint: ## ruff, format check, mypy, shellcheck/actionlint
	scripts/lint.sh

test: ## Full test suite with coverage gate
	scripts/test.sh all

build: ## Build wheels only
	scripts/build.sh --no-docker

images: ## Build wheels and container images for all apps
	scripts/build.sh

smoke: ## Offline smoke test of APP's local image
	scripts/smoke.sh $(APP)

secrets: ## Scan git history for secrets
	scripts/secret-scan.sh

ci: lint test secrets images ## Run the CI pipeline locally
	scripts/smoke.sh snap-bot
	scripts/smoke.sh status-bot

deploy: ## Promote TAG of APP to ENV (registry login required)
	scripts/deploy.sh $(APP) $(ENV) $(TAG)

rollback: ## Roll APP in ENV back to TAG (default: previous)
	scripts/rollback.sh $(APP) $(ENV) $(TAG)

clean: ## Remove build and test outputs
	rm -rf dist reports .coverage .mypy_cache .ruff_cache .pytest_cache
