FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1

WORKDIR /app

# The semantic cache (SPEC Phase 2.4) is an optional build, for a gateway that
# turns the layer on: `docker compose -f docker-compose.yml -f
# docker-compose.semantic.yml build`. It adds PyTorch (the CPU-only wheel) and
# sentence-transformers at the versions B4 measured, and bakes both models in
# at the revisions B4 measured, so startup never needs the network
# (HF_HUB_OFFLINE). These layers come before the code, so changing the code
# doesn't download them again.
ARG SEMANTIC=false
ARG SEMANTIC_MODELS="sentence-transformers/all-MiniLM-L6-v2@1110a243fdf4706b3f48f1d95db1a4f5529b4d41 cross-encoder/nli-deberta-v3-xsmall@a150876415327c80daeff35ca6f68f5ed8cf5c24"
RUN if [ "$SEMANTIC" = "true" ]; then \
      pip install --timeout 120 --retries 10 --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
      && pip install --timeout 120 --retries 10 sentence-transformers==6.2.0 transformers==5.22.1 tokenizers==0.23.3 \
           huggingface_hub==2.4.0 safetensors==0.8.0; \
    fi
COPY scripts/fetch_models.py ./scripts/fetch_models.py
RUN if [ "$SEMANTIC" = "true" ]; then \
      HF_HUB_OFFLINE=0 python scripts/fetch_models.py $SEMANTIC_MODELS; \
    fi
# No progress bars or library chatter in the gateway's logs as the models load.
ENV HF_HUB_DISABLE_PROGRESS_BARS=1 \
    TRANSFORMERS_VERBOSITY=error

COPY pyproject.toml ./
COPY src ./src
RUN pip install --timeout 120 --retries 10 .
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
