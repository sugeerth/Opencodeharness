# The image `openagent ... --sandbox docker` runs OpenCode and the checks in: Python 3.13 with
# pytest, Node 22, Go 1.24 and OpenCode, enough for both real-world benchmarks.
# Build it with `openagent sandbox build`, or extend it with your project's toolchain:
#   FROM openagent-runner:latest
#   RUN pip install -r requirements.txt
FROM node:22-bookworm-slim AS node
FROM golang:1.24-bookworm AS go

FROM python:3.13-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=go /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" GOPATH=/oa-home/go GOFLAGS=-mod=mod
# Behind a TLS-intercepting proxy, `openagent sandbox build` mounts its CA bundle as a build secret,
# used for these downloads only and not kept in the image.
RUN --mount=type=secret,id=extraca \
    ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s /usr/local/go/bin/go /usr/local/bin/go && ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt \
 && if [ -s /run/secrets/extraca ]; then \
      export NODE_EXTRA_CA_CERTS=/run/secrets/extraca PIP_CERT=/run/secrets/extraca; fi \
 && npm install -g opencode-ai@1.19.3 \
 && pip install --no-cache-dir pytest freezegun hypothesis
ENV OPENCODE_DISABLE_AUTOUPDATE=1
WORKDIR /work
