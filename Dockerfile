FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

ENV KAFKA_BROKERS=kafka:9092
ENV KAFKA_INPUT_TOPIC=vitals.raw
ENV KAFKA_OUTPUT_TOPIC=vitals.clinical
ENV KAFKA_GROUP_ID=vitals-pipeline
ENV KAFKA_DEBUG_TOPIC=
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379
ENV REDIS_TLS=false
# Optional Redis settings:
# ENV REDIS_PASSWORD=
# ENV REDIS_URL=redis://redis:6379

CMD ["python", "kafka_consumer.py"]
