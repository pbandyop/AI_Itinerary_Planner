# Agent service image for Railway / container hosts.
# Build from repo root:
#   docker build -f services/agent/Dockerfile -t itinerary-agent .

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/services/agent/src \
    AGENT_HOST=0.0.0.0 \
    RAG_EMBEDDINGS=bm25

WORKDIR /app

# System deps for sentence-transformers / chromadb (kept light; BM25 is default on Railway)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/agent/pyproject.toml services/agent/README.md services/agent/requirements.txt /app/services/agent/
COPY services/agent/src /app/services/agent/src

# Geo + RAG corpus (chroma is optional; BM25 uses chunks.jsonl)
COPY data/india_cities.json /app/data/india_cities.json
COPY data/pois /app/data/pois
COPY data/rag/corpus /app/data/rag/corpus
COPY data/rag/chunking_strategy.json /app/data/rag/chunking_strategy.json
COPY data/rag/retrieval_strategy.json /app/data/rag/retrieval_strategy.json
COPY data/rag/README.md /app/data/rag/README.md

WORKDIR /app/services/agent
RUN pip install --upgrade pip && pip install -e .

# Build BM25 chunks at image build time (no embedding model required)
RUN python -c "from agent.rag.chunking import ensure_chunks; n=len(ensure_chunks(force=True)); print(f'chunks={n}')"

ENV PORT=8000
EXPOSE 8000

# Railway sets $PORT at runtime
CMD ["sh", "-c", "python -m agent.main --serve --host 0.0.0.0 --port ${PORT:-8000}"]
