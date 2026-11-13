FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/bisbot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bisbot ./bisbot
COPY menu.md menu_targets.json ./

# The SQLite file lives in /data - mount a persistent volume there in production.
RUN mkdir -p /data

CMD ["python", "-m", "bisbot"]
