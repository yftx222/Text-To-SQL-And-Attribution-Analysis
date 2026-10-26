export UV_PROJECT_ENVIRONMENT ?= $(abspath ../.venv)

.PHONY: run worker beat init-db bootstrap-admin test

run:
	uv run python -m dataagent.main

worker:
	uv run celery --app dataagent.engines.common.tasks.celery_app:celery_app worker -l INFO

beat:
	uv run celery --app dataagent.engines.common.tasks.celery_app:celery_app beat -l INFO --schedule ../celerybeat-schedule

init-db:
	uv run scripts/init_db.py

bootstrap-admin:
	uv run -m scripts.bootstrap_admin

test:
	uv run python -X utf8 -m pytest
