FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    python3.11 python3.11-venv python3-pip \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# PyTorch with CUDA
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu121

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY api/ api/
COPY ui/ ui/
COPY models/ models/
COPY scripts/ scripts/

ENV BACKEND_MODE=remote
ENV EMBED_DEVICE=cuda
ENV LLM_BASE_URL=http://host.docker.internal:11434

EXPOSE 8000 7860

CMD ["python", "-m", "uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
