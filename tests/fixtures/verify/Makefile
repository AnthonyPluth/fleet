# The self-test's stand-in for an app's `make control-smoke` or `make verify`: it checks the environment verify.yml set
# up (Poetry's virtualenv, the npm packages, libfaketime) and writes a marker, which is what the self-test looks for.
.PHONY: smoke

smoke:
	poetry run python -c "import sys; print('python', sys.version.split()[0])"
	test -d frontend/node_modules
	command -v faketime
	mkdir -p artifacts
	echo "verify.yml ran make smoke" > artifacts/marker
	test -f artifacts/marker
