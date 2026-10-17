FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tipster ./tipster
RUN useradd -m -u 1000 tipster && mkdir -p /app/data /app/logs && chown -R tipster /app
USER tipster

VOLUME ["/app/data", "/app/logs"]
CMD ["python", "-m", "tipster"]
