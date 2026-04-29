.PHONY: sentinel-check schema-lint test help

PYTHON ?= python3

help:
	@echo "Targets:"
	@echo "  sentinel-check  Run R17 sentinel pack against the static lint engine"
	@echo "  schema-lint     Run schema + rule-registry linter (tools/lint_schemas.py)"
	@echo "  test            Run the in-tree test suite"

sentinel-check:
	$(PYTHON) tools/sentinel_check.py

schema-lint:
	$(PYTHON) tools/lint_schemas.py

test:
	$(PYTHON) -m unittest discover -s tests -v
