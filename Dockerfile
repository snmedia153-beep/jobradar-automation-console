FROM mcr.microsoft.com/playwright/python:v1.54.0-noble

WORKDIR /app

RUN mkdir -p /app/output /app/output/logs /app/output/screenshots /app/output/sessions \
    && chmod -R 775 /app/output

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV HEADLESS=true
ENV OUTPUT_DIR=/app/output
ENV DATABASE_URL=/app/output/jobradar.sqlite3
ENV EMULATOR_SLOTS=5
ENV APPIUM_HOST=host.docker.internal
ENV APPIUM_CONNECT_HOST=host.docker.internal
ENV JOBRADAR_DOCKER_MODE=true
ENV REDIS_URL=redis://redis:6379/0
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

CMD ["python", "-m", "jobradar.cli", "worker", "--once", "--max-jobs", "4"]
