FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libjpeg-dev zlib1g-dev libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg_trade_bot/ ./

# Create output and data directories (data is mounted as volume in production)
RUN mkdir -p images output data

ENV PYTHONUNBUFFERED=1

CMD ["python3", "main.py"]
