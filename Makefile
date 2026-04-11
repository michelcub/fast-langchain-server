.PHONY: install dev test lint run clean

install:
	uv pip install -e .

dev:
	uv pip install -e ".[dev]"

test:
	pytest tests/ -v

lint:
	ruff check fast_langchain_server/ tests/

run:
	AGENT_NAME=dev-agent \
	MODEL_API_URL=http://localhost:11434/v1 \
	MODEL_NAME=llama3.2 \
	fast-langchain-server run agent.py --reload

docker-build:
	docker build -t langchain-agent-server .

docker-run:
	docker run --rm -p 8000:8000 \
	  -e AGENT_NAME=my-agent \
	  -e MODEL_API_URL=http://host.docker.internal:11434/v1 \
	  -e MODEL_NAME=llama3.2 \
	  -v $(PWD)/agent.py:/app/agent.py \
	  langchain-agent-server

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete 2>/dev/null; true
	rm -rf .pytest_cache dist build *.egg-info
