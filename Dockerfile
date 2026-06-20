FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

COPY src/ /app/src/

RUN addgroup -S app \
    && adduser -S app -G app \
    && mkdir -p /data/cache /data/output /data/state \
    && chown -R app:app /data /app

USER app

VOLUME ["/data"]

CMD ["python", "-m", "tlm_auto_download"]
