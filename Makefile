.PHONY: install check lint format type test validate serve

install:
	uv sync

check: lint type test

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

type:
	uv run mypy src

test:
	uv run pytest

validate:
	uv run code-triage-agent validate-config

serve:
	uv run code-triage-agent serve --reload
