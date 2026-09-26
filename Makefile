.PHONY: docs test check coverage benchmark

docs:
	uv run --locked docs

test:
	uv run --locked pytest

check:
	uv run --locked prek run --all-files

benchmark:
	uv run --locked benchmark

coverage:
	uv run --locked python -m slipcover --source src/pyprom_exporters --xml --out coverage.xml -m pytest
	uv run --locked genbadge coverage -i coverage.xml -o coverage.svg -l
