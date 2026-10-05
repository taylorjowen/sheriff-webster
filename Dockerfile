FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY sheriff ./sheriff
COPY scenarios ./scenarios

# Persistent state (live DB, archive, answer cache, pattern table, harvested glyphs)
# lives in /app/data -- mount a volume there. config.yaml is mounted at /app/config.yaml.
VOLUME ["/app/data"]

CMD ["python", "-m", "sheriff"]
