# The self-test's stand-in for an app's `make control-smoke` or `make verify`: it checks the environment verify.yml set
# up (Poetry's virtualenv, Node from .nvmrc and the npm project, libfaketime) and writes a marker, which is what the self-test looks for.
.PHONY: smoke

smoke:
	poetry run python -c "import sys; print('python', sys.version.split()[0])"
	node --version
	npm --prefix frontend ls
	dpkg -s libfaketime
	mkdir -p artifacts
	echo "verify.yml ran make smoke" > artifacts/marker
	test -f artifacts/marker
