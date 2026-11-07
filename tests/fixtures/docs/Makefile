# The self-test's stand-in for an app's `make docs-build`: it checks the Node docs.yml set up, installs docs/'s
# (empty) packages and writes the one page the workflow's check looks for.
.PHONY: docs-build

docs-build:
	node --version
	cd docs && npm ci --no-audit --no-fund
	mkdir -p docs/dist
	echo '<!doctype html><title>docs fixture</title>' > docs/dist/index.html
