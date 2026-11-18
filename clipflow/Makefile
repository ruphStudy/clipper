install:
	scripts/mac/install.sh

uninstall:
	scripts/mac/uninstall.sh

status:
	scripts/mac/status.sh

restart:
	scripts/mac/restart.sh all

logs:
	scripts/mac/logs.sh app

test:
	.venv/bin/python -m pytest -q

check:
	.venv/bin/python -m app.main check

.PHONY: install uninstall status restart logs test check
