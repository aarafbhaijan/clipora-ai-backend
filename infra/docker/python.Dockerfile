# One Python image, several entrypoints (architecture.md section 4).
#   API:     uvicorn api.main:app --host 0.0.0.0 --port 8000
#   Workers: python -m worker.run --queue ingest|agent|export --slots N
# The API target installs only clipora-api, so no media or ML packages land in it.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY backend backend
COPY database database

FROM base AS api
RUN uv sync --frozen --no-dev --package clipora-api
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
# fonts-dejavu-core: burned-in captions use DejaVu Sans.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen --no-dev --package clipora-worker
ENV PATH="/app/.venv/bin:$PATH" \
    YOLO_MODEL=/models/yolov8n.pt \
    HF_HOME=/models/hf \
    WORK_DIR=/work
# Bake model weights into the image so workers never download at job time.
RUN mkdir -p /models /work && cd /models \
    && python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" \
    && python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')" \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"
# WORK_DIR must be on a volume with free space of at least twice the source size (architecture.md section 13).
CMD ["python", "-m", "worker.run", "--queue", "agent", "--slots", "4"]
