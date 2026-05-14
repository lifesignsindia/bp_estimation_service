FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

ENV KAFKA_BROKERS=kafka:9092
ENV KAFKA_INPUT_TOPIC=vitals.raw
ENV KAFKA_OUTPUT_TOPIC=vitals.clinical
ENV REDIS_HOST=redis
ENV REDIS_PORT=6379

CMD ["python", "kafka_consumer.py"]
