FROM python:3.9-slim

WORKDIR /app

# Install required system packages for serial communication
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application
COPY s8.py .

# Environment variables with defaults
ENV MQTT_HOST=localhost \
    MQTT_PORT=1883 \
    MQTT_TOPIC_PREFIX=sensors/co2 \
    SERIAL_PORT=/dev/ttyAMA0 \
    PROMETHEUS_PORT=9100

# Expose prometheus metrics port
EXPOSE ${PROMETHEUS_PORT}

# Run the application
CMD ["python", "s8.py"]
