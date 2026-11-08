# Everyday tasks. Everything runs in the uv project environment (Python 3.12, see
# pyproject.toml); `make setup` creates it.

.PHONY: setup test lint types check

setup:            ## create/refresh .venv with every extra and the dev tools
	uv sync --all-extras

test:             ## the whole test suite
	uv run pytest -q

lint:             ## pyflakes-level defects (ruff)
	uv run ruff check

types:            ## static types (pyright, basic mode)
	uv run pyright

check: test lint  ## before committing
