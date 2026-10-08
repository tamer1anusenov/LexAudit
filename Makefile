UV ?= uv

.PHONY: install test run

## Install project (editable) + dev dependencies into .venv
install:
	$(UV) sync --extra dev

## Run the test suite
test:
	$(UV) run --extra dev pytest -q

## Run the CLI (scaffold stage: prints version/help; re-pointed at the
## pipeline / API when those tasks land)
run:
	$(UV) run python -m lexaudit
