.PHONY: up down test test-db serve lint
up:        ; docker compose up -d db
down:      ; docker compose down
# Needs DATABASE_URL exported (set -a; . ./.env; set +a); `cpsim serve` applies the migrations first.
serve:     ; .venv/bin/cpsim serve
lint:      ; .venv/bin/ruff check src tests
test:      ; .venv/bin/pytest -q
# Needs POSTGRES_PASSWORD exported (set -a; . ./.env; set +a). Uses a throwaway database named cpsim_test on the compose Postgres.
test-db:   ; docker compose exec -T db psql -U cpsim -c "DROP DATABASE IF EXISTS cpsim_test" -c "CREATE DATABASE cpsim_test" && \
	TEST_DATABASE_URL=postgresql://cpsim:$$POSTGRES_PASSWORD@127.0.0.1:5436/cpsim_test .venv/bin/pytest -q
