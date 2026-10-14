FROM python:3.11-slim

ARG APP_BUILD_SHA=dev
ENV APP_BUILD_SHA=${APP_BUILD_SHA}

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo-dev zlib1g-dev fonts-dejavu-core lbzip2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN printf '%s\n' "$APP_BUILD_SHA" > /app/BUILD_SHA

RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup appuser && \
    mkdir -p /app/images /app/og_cache /app/reports && \
    chown -R appuser:appgroup /app /app/images /app/og_cache /app/reports

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--limit-concurrency", "80", "--timeout-keep-alive", "5"]
