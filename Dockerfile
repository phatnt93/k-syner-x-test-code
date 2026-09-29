# One image for every CDMS role (docs/architecture.md, D12): the command decides what runs —
#   api       uvicorn cdms.api.main:app
#   worker    python -m cdms.worker
#   emulator  uvicorn cdms.emulator.main:app
#   migrate   alembic upgrade head
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first: this layer is reused as long as requirements.txt does not change.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src

RUN useradd --system --uid 10001 cdms && chown -R cdms /app
USER cdms

EXPOSE 8100 8101
CMD ["uvicorn", "cdms.api.main:app", "--host", "0.0.0.0", "--port", "8100"]
