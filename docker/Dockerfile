# syntax=docker/dockerfile:1.7
# One image per app: docker build -f docker/Dockerfile --build-arg APP=snap-bot .
ARG BASE_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12

FROM ${UV_IMAGE} AS uv

FROM ${BASE_IMAGE} AS build
ARG APP
RUN test -n "${APP}" || (echo "APP build-arg is required" >&2; exit 1)
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps ./apps
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${APP}"

FROM ${BASE_IMAGE} AS runtime
ARG APP
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="${APP}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/Khalidshar/snap-status-v3"
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin bot
COPY --from=build /opt/venv /opt/venv
COPY config /app/config
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STUDIO_CONFIG=/app/config/defaults.toml \
    APP_ENTRYPOINT=${APP} \
    APP_VERSION=${VERSION}
WORKDIR /app
USER 10001
ENTRYPOINT ["/bin/sh", "-c", "exec \"$APP_ENTRYPOINT\" \"$@\"", "--"]
CMD ["healthcheck"]
