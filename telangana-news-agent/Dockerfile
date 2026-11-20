FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LOG_FORMAT=json

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY config ./config
RUN pip install . && useradd --create-home runner
USER runner

# Cloud Run Jobs override the command per job:
#   monitor: python -m app.monitor
#   digest:  python -m app.digest
CMD ["python", "-m", "app.monitor"]
