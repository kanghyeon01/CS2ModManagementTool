# cs2mmt-sentinel: a fully static C binary on a tiny Alpine image (multi-arch).
#   docker build -t cs2mmt-sentinel sentinel/
# Just the static binary, for Synology without Docker (copy it to the NAS):
#   docker build --target export -o out sentinel/
FROM alpine:3.20 AS build
RUN apk add --no-cache build-base
WORKDIR /src
COPY Makefile ./
COPY src ./src
COPY tests ./tests
RUN make test CC=cc SAN= && make clean && make static CC=cc CFLAGS="-O2 -g0" && strip cs2mmt-sentinel && ./cs2mmt-sentinel --version

FROM scratch AS export
COPY --from=build /src/cs2mmt-sentinel /cs2mmt-sentinel

FROM alpine:3.20
# build-base: the entrypoint rebuilds from the mounted repository after a tool update
RUN apk add --no-cache curl ca-certificates tzdata build-base && adduser -S -u 10002 -h /data sentinel
COPY --from=build /src/cs2mmt-sentinel /usr/local/bin/cs2mmt-sentinel
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER sentinel
VOLUME ["/data", "/config"]
EXPOSE 8766
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=5 \
    CMD curl -fsS -o /dev/null --max-time 8 http://127.0.0.1:8766/api/v1/health || exit 1
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "--config", "/config/sentinel.conf"]
