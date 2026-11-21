.PHONY: setup migrate dev test lint fmt typecheck check e2e plists start stop restart logs

# Office-Mac operations (docs/operations.md). These targets are for the operator to run by hand;
# start, stop and restart talk to launchd in the current user's GUI session.
LAUNCH_AGENTS := $(HOME)/Library/LaunchAgents
LOG_DIR := $(HOME)/Library/Logs/SalesFinder
WEB_LABEL := com.elchemy.salesfinder.web
WORKER_LABEL := com.elchemy.salesfinder.worker
GUI := gui/$$(id -u)
# Polls (up to 11 minutes, past the worker's 600 s ExitTimeOut) until neither agent is loaded.
WAIT_UNLOADED := for i in $$(seq 660); do launchctl print $(GUI)/$(WORKER_LABEL) >/dev/null 2>&1 || launchctl print $(GUI)/$(WEB_LABEL) >/dev/null 2>&1 || exit 0; sleep 1; done; echo "SalesFinder is still loaded after 11 minutes: check make logs." >&2; exit 1
# launchd appends to the crash logs for as long as an agent is loaded, so they would only grow.
# Once both are unloaded (nothing holds them), each non-empty one becomes *.crash.log.prev,
# replacing the older copy: the last crash output is kept, and at most two of each exist.
CLEAR_CRASH_LOGS := for f in $(LOG_DIR)/web.crash.log $(LOG_DIR)/worker.crash.log; do if [ -s "$$f" ]; then mv -f "$$f" "$$f.prev"; fi; done

setup:
	uv sync
	uv run pre-commit install
	@psql -lqt | cut -d\| -f1 | grep -qw salesfinder || createdb salesfinder
	@psql -lqt | cut -d\| -f1 | grep -qw salesfinder_test || createdb salesfinder_test
	@test -f .env || cp .env.example .env

migrate:
	uv run salesfinder db upgrade

dev:
	uv run salesfinder web --reload

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src

check: lint typecheck test

e2e:
	uv run pytest tests/test_e2e.py -q

# Render both LaunchAgents into ~/Library/LaunchAgents (refuses to overwrite; see the docs).
plists:
	uv run salesfinder ops plists --out $(LAUNCH_AGENTS)

# Load both agents: they start now and at every login, and launchd restarts them if they exit.
start:
	launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WEB_LABEL).plist
	launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WORKER_LABEL).plist

# Unload both, the worker first. launchd sends SIGTERM: the worker finishes its current pass
# (its Claude steps and discovery start nothing new) within the plist's ExitTimeOut (600 s).
# One not loaded is fine. Then wait until both are gone, so `start` can load them again, and
# set the crash logs aside.
stop:
	-launchctl bootout $(GUI)/$(WORKER_LABEL)
	-launchctl bootout $(GUI)/$(WEB_LABEL)
	@$(WAIT_UNLOADED)
	@$(CLEAR_CRASH_LOGS)

# Restart both together, always (never one alone: they must run the same code): stop, then
# start, so a re-rendered plist is read again.
restart:
	-launchctl bootout $(GUI)/$(WORKER_LABEL)
	-launchctl bootout $(GUI)/$(WEB_LABEL)
	@$(WAIT_UNLOADED)
	@$(CLEAR_CRASH_LOGS)
	launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WEB_LABEL).plist
	launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WORKER_LABEL).plist

# The app's logs and launchd's crash output (a traceback that bypassed logging).
logs:
	tail -F $(LOG_DIR)/web.log $(LOG_DIR)/worker.log $(LOG_DIR)/web.crash.log $(LOG_DIR)/worker.crash.log
