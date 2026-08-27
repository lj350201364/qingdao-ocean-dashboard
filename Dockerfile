FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVER_MODE=1 \
    PORT=7860 \
    OCEAN_NOTIFICATION_CONFIG=/data/notification_config.json \
    OCEAN_NOTIFICATION_DB=/data/ocean_notifications.db

WORKDIR /app

COPY OceanWindow_optimized.py ocean_notifications.py notification_config.json ./
COPY web ./web

RUN mkdir -p /data && useradd --create-home --uid 10001 ocean && chown -R ocean:ocean /app /data

USER ocean
EXPOSE 7860

CMD ["python", "OceanWindow_optimized.py", "--server"]
