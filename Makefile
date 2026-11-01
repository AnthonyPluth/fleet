# The checks to run before you push: `make check` runs ruff, mypy, the tests, fleet's own self-check, actionlint,
# zizmor and `claude plugin validate`, each once; "fleet CI" (.github/workflows/ci.yml) runs the same. `make setup` first, on a fresh clone.
# Python commands go through Poetry, at the tool versions pyproject.toml's dev group pins.

PYTHON ?= poetry run python
RUFF ?= poetry run ruff
MYPY ?= poetry run mypy
FLEET ?= poetry run fleet
ZIZMOR ?= poetry run zizmor
ACTIONLINT ?= .tools/actionlint
CLAUDE ?= .tools/claude-code/node_modules/.bin/claude

# actionlint isn't a Python package, so `make setup` downloads its release binary, pinned by version and SHA-256
# checksum per platform. Change all of them together.
ACTIONLINT_VERSION := 1.7.12
ACTIONLINT_SHA256_linux_amd64 := 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
ACTIONLINT_SHA256_linux_arm64 := 325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6
ACTIONLINT_SHA256_darwin_amd64 := 5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644
ACTIONLINT_SHA256_darwin_arm64 := aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f
ACTIONLINT_OS := $(shell uname -s | tr '[:upper:]' '[:lower:]')
ACTIONLINT_ARCH := $(subst aarch64,arm64,$(subst x86_64,amd64,$(shell uname -m)))
ACTIONLINT_PLATFORM := $(ACTIONLINT_OS)_$(ACTIONLINT_ARCH)
ACTIONLINT_TARBALL := actionlint_$(ACTIONLINT_VERSION)_$(ACTIONLINT_PLATFORM).tar.gz

.PHONY: setup check lint test self-check workflows plugin fix

check: lint test self-check workflows plugin

# A fresh clone's dependencies: Poetry's environment (the dev tools, and fleet_tools itself for the `fleet` command)
# the pinned actionlint, and the Claude Code the agent review locks (.github/agent-review/), for `make plugin`.
setup: .tools/actionlint .tools/claude-code/node_modules/.bin/claude
	poetry install --no-interaction

lint:
	$(RUFF) check .
	$(MYPY)

test:
	$(PYTHON) -m unittest discover tests

self-check:
	$(FLEET) self-check

# actionlint and zizmor over fleet's workflows. zizmor runs its online audits only when GH_TOKEN is set (as in CI);
# without it, --offline keeps a local run from failing on GitHub's API limits.
ZIZMOR_FLAGS ?= $(if $(GH_TOKEN),,--offline)
workflows:
	$(ACTIONLINT)
	$(ZIZMOR) $(ZIZMOR_FLAGS) .github/workflows/

# The marketplace and the plugin (its manifest, hooks, skills and agent), warnings included.
plugin:
	$(CLAUDE) plugin validate --strict .
	$(CLAUDE) plugin validate --strict plugins/fleet
	$(CLAUDE) plugin validate --strict plugins/fleet/skills
	$(CLAUDE) plugin validate --strict plugins/fleet/agents

# Applies ruff's safe fixes (unused imports and the like); review the diff before committing.
fix:
	$(RUFF) check --fix .

.tools/actionlint:
	@test -n "$(ACTIONLINT_SHA256_$(ACTIONLINT_PLATFORM))" || { echo "No pinned actionlint for $(ACTIONLINT_PLATFORM)" >&2; exit 1; }
	mkdir -p .tools
	curl -fsSL -o .tools/$(ACTIONLINT_TARBALL) https://github.com/rhysd/actionlint/releases/download/v$(ACTIONLINT_VERSION)/$(ACTIONLINT_TARBALL)
	echo "$(ACTIONLINT_SHA256_$(ACTIONLINT_PLATFORM))  .tools/$(ACTIONLINT_TARBALL)" | shasum -a 256 -c -
	tar -xzf .tools/$(ACTIONLINT_TARBALL) -C .tools actionlint
	rm .tools/$(ACTIONLINT_TARBALL)

.tools/claude-code/node_modules/.bin/claude: .github/agent-review/package.json .github/agent-review/package-lock.json
	mkdir -p .tools/claude-code
	cp .github/agent-review/package.json .github/agent-review/package-lock.json .tools/claude-code/
	cd .tools/claude-code && npm ci --no-audit --no-fund
	touch $@
