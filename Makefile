PYTHON ?= .venv/bin/python
COMPOSE ?= $(shell if command -v podman >/dev/null 2>&1; then echo podman compose; else echo docker compose; fi)
CONTROLLER_PORT ?= 8000
CONFIRM = $(if $(filter 1,$(YES)),--yes,)

.PHONY: dev-up dev-down dev-db-up dev-migrate dev-controller dev-reset dev-status dev-state dev-seed-workers dev-seed-jobs dev-heartbeats check-engines test-scheduler test-scheduler-basic test-scheduler-concurrency examples-check examples-build-podman examples-build-docker

dev-db-up:
	$(COMPOSE) up -d postgres

dev-migrate:
	$(PYTHON) -m devtools.lab wait-db
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
	$(PYTHON) -m devtools.lab reset $(CONFIRM)

dev-status:
	$(PYTHON) -m devtools.lab status

dev-state:
	$(PYTHON) -m devtools.lab state

dev-seed-workers:
	$(PYTHON) -m devtools.lab seed-workers

dev-seed-jobs:
	$(PYTHON) -m devtools.lab seed-jobs

dev-heartbeats:
	$(PYTHON) -m devtools.lab heartbeat --loop

check-engines:
	$(PYTHON) -m devtools.engines

test-scheduler:
	$(PYTHON) -m devtools.lab scenario all $(CONFIRM)

test-scheduler-basic:
	$(PYTHON) -m devtools.lab scenario basic $(CONFIRM)

test-scheduler-concurrency:
	$(PYTHON) -m devtools.lab scenario concurrency $(CONFIRM)

examples-check:
	$(PYTHON) -m devtools.examples check

examples-build-podman:
	$(PYTHON) -m devtools.examples build --engine podman

examples-build-docker:
	$(PYTHON) -m devtools.examples build --engine docker

.PHONY: examples-run-podman check-container-limits dev-real-worker test-real-worker
examples-run-podman:
	$(PYTHON) -m devtools.examples run --engine podman

check-container-limits:
	$(PYTHON) -m devtools.examples limits --engine podman

dev-real-worker:
	$(PYTHON) -m devtools.real_worker start

test-real-worker:
	$(PYTHON) -m devtools.real_worker test
