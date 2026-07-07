.PHONY: venv dev test lint clean validate-content import-content replay

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

venv:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	$(PIP) install -e ".[dev]" -q

dev: venv
	$(PY) -m uvicorn server.app:app --reload --host $${GAME_HOST:-0.0.0.0} --port $${GAME_PORT:-8100}

test: venv
	$(PY) -m pytest -q

validate-content: venv
	$(PY) scripts/validate_content.py

import-content: venv
	$(PY) scripts/import_content.py $(ARGS)

replay: venv
	$(PY) scripts/replay.py $(ARGS)

clean:
	rm -rf $(VENV) .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
