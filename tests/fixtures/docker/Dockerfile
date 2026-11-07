# The Self-test's image for docker.yml's build: a static file server on 8765 that answers /healthz, and starts only
# when the start check passes it FLEET_SMOKE=on (smoke-env), so the check fails if the variables don't reach it.
FROM mirror.gcr.io/library/busybox:1.37.0
ARG VERSION=""
RUN mkdir -p /www && echo ok > /www/healthz && echo "${VERSION}" > /www/version
USER 65534:65534
EXPOSE 8765
HEALTHCHECK NONE
CMD ["sh", "-c", "[ \"$FLEET_SMOKE\" = on ] || { echo 'FLEET_SMOKE is not on' >&2; exit 1; }; exec httpd -f -p 8765 -h /www"]
