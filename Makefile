.PHONY: help bootstrap up down test lint fmt

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

bootstrap: ## Create venv and install all dependencies
	bash scripts/bootstrap.sh

up: ## Start all infrastructure (postgres, mongo, influx, minio, redis, grafana)
	docker compose up -d

down: ## Stop all infrastructure
	docker compose down

test: ## Run test suite
	pytest

lint: ## Run ruff + mypy
	ruff check .
	mypy .

fmt: ## Auto-format code
	ruff format .
	ruff check --fix .

clean: ## Remove .venv and __pycache__
	rm -rf .venv
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
