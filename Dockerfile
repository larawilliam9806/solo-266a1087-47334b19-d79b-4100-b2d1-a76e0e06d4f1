FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    LIVE_DB=/app/data/live.db \
    BACKUP_DB=/app/data/backup.db \
    AUTO_SEED=1 \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data

EXPOSE 8000

# 启动前若数据库不存在则自动写入演示数据，随后用 gunicorn 提供服务
CMD ["sh", "-c", "python -m app.seed && gunicorn --workers 1 --threads 8 --bind 0.0.0.0:${PORT} --timeout 60 wsgi:app"]
