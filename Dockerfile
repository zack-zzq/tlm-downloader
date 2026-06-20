FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

COPY src/ /app/src/

RUN mkdir -p /data/cache /data/output /data/state

USER root

VOLUME ["/data"]

CMD ["python", "-m", "tlm_auto_download"]
