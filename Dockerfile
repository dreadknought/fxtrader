# file: Dockerfile
FROM python:3.12-slim

# Keep Python logs visible immediately in CloudWatch Logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime dependencies first so Docker layer caching works well.
COPY pyproject.toml ./
RUN python -m pip install --upgrade pip \
    && python -m pip install .

# Copy source after dependency install.
COPY src ./src
COPY scripts/fargate-entrypoint.sh ./scripts/fargate-entrypoint.sh
RUN chmod +x ./scripts/fargate-entrypoint.sh

CMD ["./scripts/fargate-entrypoint.sh"]
