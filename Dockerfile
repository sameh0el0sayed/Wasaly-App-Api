# Multi-stage: dependencies are built once into a venv (cached until requirements.txt changes), runtime stays slim.

FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# test-only packages stay out of the production image
RUN grep -vE '^(pytest|pytest-asyncio)([=<> ]|$)' /tmp/requirements.txt > /tmp/req.txt \
 && /opt/venv/bin/pip install -r /tmp/req.txt

FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=deps /opt/venv /opt/venv
COPY . .
# a broken image fails the build, never production (defaults to sqlite settings; no network, no DB connection at import)
RUN python -c "import app.main" && python -m compileall -q app alembic scripts
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
