FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
# Bake tokenizer files into the image so startup never needs the network.
RUN python -c "import tiktoken; [tiktoken.get_encoding(e) for e in ('o200k_base', 'cl100k_base')]"
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

EXPOSE 8000
# On stop, streams still running after 5 s are cut off and recorded as
# gateway_shutdown, before Docker's SIGKILL (10 s by default) loses them.
ENV UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=5
# Idle keep-alive connections are kept longer than any client keeps them
# (nginx's upstream pool: 60 s; httpx and the OpenAI SDK: 5 s), so a client
# never sends a request down a connection the gateway has just closed; nginx
# would answer a POST lost that way with a 502, as it never retries one.
ENV UVICORN_TIMEOUT_KEEP_ALIVE=75
# uvicorn takes its worker count from WEB_CONCURRENCY.
CMD ["uvicorn", "--factory", "tollgate.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
