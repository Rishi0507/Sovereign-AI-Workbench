# Thin wrapper around scripts/dev.py so the same targets work on Linux, macOS and Windows.
PYTHON ?= python

.PHONY: setup fixtures test lint demo serve chat-model eval render-serve go-build go-test go-lint go-integration clean

setup:
	$(PYTHON) scripts/dev.py setup

fixtures:
	$(PYTHON) scripts/dev.py fixtures

test:
	$(PYTHON) scripts/dev.py test

lint:
	$(PYTHON) scripts/dev.py lint

demo:
	$(PYTHON) scripts/dev.py demo

serve:
	$(PYTHON) scripts/dev.py serve

chat-model:
	$(PYTHON) scripts/dev.py chat-model

eval:
	$(PYTHON) scripts/dev.py eval

render-serve:
	$(PYTHON) scripts/dev.py render-serve

go-build:
	$(PYTHON) scripts/dev.py go-build

go-test:
	$(PYTHON) scripts/dev.py go-test

go-lint:
	$(PYTHON) scripts/dev.py go-lint

go-integration:
	$(PYTHON) scripts/dev.py go-integration

clean:
	$(PYTHON) scripts/dev.py clean
