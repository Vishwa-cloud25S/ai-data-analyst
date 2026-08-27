.PHONY: help install seed api ui dev test lint fmt docker-up docker-down clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install dev dependencies
	pip install -r requirements-dev.txt

seed: ## Build the local DuckDB warehouse
	python -m app.db.seed

api: seed ## Run the FastAPI service on :8000
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

ui: ## Run the Streamlit app on :8501
	streamlit run ui/streamlit_app.py --server.address=0.0.0.0 --server.port=8501

test: ## Run the test suite
	pytest -q --cov=app --cov-report=term-missing

lint: ## Lint with ruff
	ruff check app ui tests scripts

fmt: ## Auto-fix lint issues
	ruff check --fix app ui tests scripts

docker-up: ## Build and start API + UI with docker compose
	docker compose up --build

docker-down:
	docker compose down -v

clean:
	rm -rf data .pytest_cache .ruff_cache htmlcov coverage.xml
	find . -name __pycache__ -type d -exec rm -rf {} +
