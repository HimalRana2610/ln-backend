# syntax=docker/dockerfile:1

# --- build stage: resolve dependencies into a virtualenv ------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what pip needs, so the layer caches until dependencies change.
COPY pyproject.toml README.md ./
COPY app/__init__.py ./app/
RUN pip install --upgrade pip && pip install .

# --- runtime stage --------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Run unprivileged.
RUN useradd --create-home --uid 1000 app

WORKDIR /srv
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
