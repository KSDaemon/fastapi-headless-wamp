.PHONY: help setup lint format test test-cov test-e2e

help:
	@echo "Available Make Targets"
	@echo "======================"
	@echo ""
	@echo "  make setup              - Setup project dependencies"
	@echo "  make lint               - Run linters (ruff check + ruff format --check)"
	@echo "  make format             - Format code (ruff format)"
	@echo "  make test               - Run all tests"
	@echo "  make test-cov           - Run all tests with coverage report (opens HTML in browser)"
	@echo "  make test-e2e           - Run end-to-end tests (examples/e2e)"

check-uv:
	@command -v uv >/dev/null 2>&1 || { echo >&2 "UV is not installed. Installing..."; curl -LsSf https://astral.sh/uv/install.sh | sh; }
	uv sync

setup: check-uv
	uv pip install -e .

lint: setup
	uv run ruff check
	uv run ruff format --check

format:
	uv run ruff format

test:
	@echo "Running tests..."
	@uv run pytest -n auto

test-cov:
	@echo "Running tests with coverage..."
	@uv run pytest -n auto --cov --cov-report=lcov
	@echo "Generating HTML coverage report..."
	@genhtml coverage.lcov -o coverage-html --ignore-errors inconsistent,corrupt,category,count
	@rm -f coverage.lcov
	@echo "Coverage report generated in coverage-html/"
	@if command -v open >/dev/null 2>&1; then \
		echo "Opening coverage report in browser..."; \
		open coverage-html/index.html; \
	else \
		echo "Open coverage-html/index.html in your browser to view the report"; \
	fi

test-e2e:
	@echo "Running end-to-end tests..."
	@bash examples/e2e/run.sh
