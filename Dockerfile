FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PATH="/root/.local/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gcc python3-dev pkg-config \
    libheif-dev libde265-dev libjpeg-dev zlib1g-dev \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system --no-cache \
    gemini_webapi \
    fastapi \
    uvicorn \
    python-multipart \
    pydantic \
    loguru \
    pillow \
    pillow-heif==0.15.0

COPY main.py .
RUN mkdir -p /app/config

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]