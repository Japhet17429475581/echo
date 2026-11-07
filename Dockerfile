FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends age fonts-dejavu-core ca-certificates tini \
    libimage-exiftool-perl tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus whatweb file libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY engines/sherlock/requirements.lock /tmp/sherlock.lock
COPY engines/maigret/requirements.lock /tmp/maigret.lock
COPY tools/requirements.lock /tmp/tools.lock
RUN uv venv /opt/sherlock && uv pip install --python /opt/sherlock/bin/python --require-hashes -r /tmp/sherlock.lock \
    && uv venv /opt/maigret && uv pip install --python /opt/maigret/bin/python --require-hashes -r /tmp/maigret.lock \
    && uv venv /opt/tools && uv pip install --python /opt/tools/bin/python --require-hashes -r /tmp/tools.lock
COPY src src
COPY engines/runner.py engines/runner.py
COPY tools tools
COPY assets/echo assets/echo
COPY migrations migrations
COPY alembic.ini ./
COPY LICENSE NOTICE THIRD_PARTY_LICENSES.md ./
COPY licenses licenses
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home echo \
    && mkdir -p /tmp/echo /data/echo && chown echo:echo /tmp/echo /data/echo && chmod 0700 /tmp/echo /data/echo
ENV PATH="/app/.venv/bin:$PATH" ECHO_TEMP_DIR=/tmp/echo
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "echo.main", "bot"]

FROM base AS test
USER root
RUN uv sync --frozen --extra dev
COPY tests tests
COPY scripts scripts
USER 10001:10001
ENV ECHO_TEST_SHERLOCK_PYTHON=/opt/sherlock/bin/python ECHO_TEST_MAIGRET_PYTHON=/opt/maigret/bin/python ECHO_TEST_LIVE_ENGINES=1 ECHO_TEST_TOOLS=1
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS runtime
