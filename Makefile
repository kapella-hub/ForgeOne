.PHONY: install test lint backtest paper clean

install:
	python -m pip install -e .[dev]

test:
	pytest tests/ -v

lint:
	ruff check src tests scripts

backtest:
	python -m forgeone.scripts.backtest_continuation_port

paper:
	python -m forgeone.strategies.hyperliquid_paper

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
