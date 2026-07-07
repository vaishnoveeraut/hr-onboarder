.PHONY: install playground run test lint

AGENT_DIR := app
HOST      := 127.0.0.1
PORT      := 18081

install:
	uv sync

playground:
	uv run adk web $(AGENT_DIR) --host $(HOST) --port $(PORT) --reload_agents

run:
	uv run uvicorn $(AGENT_DIR).fast_api_app:app --host $(HOST) --port 8000 --reload

test:
	uv run pytest tests/unit tests/integration -v

lint:
	uv run ruff check $(AGENT_DIR)
