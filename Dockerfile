FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
COPY config ./config

EXPOSE 8000
# uvicorn takes its worker count from WEB_CONCURRENCY.
CMD ["uvicorn", "--factory", "tollgate.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
