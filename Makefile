PYTHON ?= .venv/bin/python
COMPOSE ?= $(shell if command -v podman >/dev/null 2>&1; then echo podman compose; else echo docker compose; fi)
CONTROLLER_PORT ?= 8000
CONFIRM = $(if $(filter 1,$(YES)),--yes,)

.PHONY: dev-up dev-down dev-db-up dev-migrate dev-controller dev-reset dev-status dev-state dev-seed-workers dev-seed-jobs dev-heartbeats check-engines test-scheduler test-scheduler-basic test-scheduler-concurrency examples-check examples-build-podman examples-build-docker

dev-db-up:
	$(COMPOSE) up -d postgres

dev-migrate:
	$(PYTHON) -m local_test.lab wait-db
	$(PYTHON) -m alembic upgrade head

dev-controller: dev-migrate
	$(PYTHON) -m uvicorn controller.main:app --host 127.0.0.1 --port $(CONTROLLER_PORT)

# Controller stays in the foreground. Ctrl-C stops it; the DB keeps its volume.
dev-up:
	$(MAKE) dev-db-up
	$(MAKE) dev-controller

dev-down:
	$(COMPOSE) down

dev-reset:
	$(PYTHON) -m local_test.lab reset $(CONFIRM)

dev-status:
	$(PYTHON) -m local_test.lab status

dev-state:
	$(PYTHON) -m local_test.lab state

dev-seed-workers:
	$(PYTHON) -m local_test.lab seed-workers

dev-seed-jobs:
	$(PYTHON) -m local_test.lab seed-jobs

dev-heartbeats:
	$(PYTHON) -m local_test.lab heartbeat --loop

check-engines:
	$(PYTHON) -m local_test.engines

test-scheduler:
	$(PYTHON) -m local_test.lab scenario all $(CONFIRM)

test-scheduler-basic:
	$(PYTHON) -m local_test.lab scenario basic $(CONFIRM)

test-scheduler-concurrency:
	$(PYTHON) -m local_test.lab scenario concurrency $(CONFIRM)

examples-check:
	$(PYTHON) -m local_test.examples check

examples-build-podman:
	$(PYTHON) -m local_test.examples build --engine podman

examples-build-docker:
	$(PYTHON) -m local_test.examples build --engine docker

.PHONY: examples-run-podman check-container-limits dev-real-worker test-real-worker
examples-run-podman:
	$(PYTHON) -m local_test.examples run --engine podman

check-container-limits:
	$(PYTHON) -m local_test.examples limits --engine podman

dev-real-worker:
	$(PYTHON) -m local_test.real_worker start

test-real-worker:
	$(PYTHON) -m local_test.real_worker test

# Recipe lines (not prerequisites) keep each aggregate ordered under make -j.
.PHONY: test-local-runtime test-local-assignment test-local
test-local-runtime:
	@echo "Requires usable Podman CLI/API and local images; see local_test/README.md for setup."
	$(PYTHON) -m local_test.engines --engine podman --require
	$(MAKE) examples-check
	$(MAKE) examples-run-podman
	$(MAKE) check-container-limits

test-local-assignment:
	@echo "Requires make dev-up and a healthy make dev-real-worker in separate terminals; inspect with make dev-state."
	$(MAKE) dev-status
	$(MAKE) test-real-worker

test-local:
	$(MAKE) test-local-runtime
	$(MAKE) test-local-assignment

# Phase 5 is explicit: unlike assignment diagnostics, completed executions release reservations.
.PHONY: dev-work-once test-execution
dev-work-once:
	$(PYTHON) -m local_test.real_worker work-once

test-execution:
	$(PYTHON) -m local_test.execution
