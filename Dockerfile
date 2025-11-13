FROM python:3.12-slim

# ставим системные библиотеки: ffmpeg, opus, libsodium
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libopus0 \
        libsodium23 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# зависимости Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код бота
COPY . .

# переменная для ffmpeg (можно и не ставить)
ENV FFMPEG_PATH=ffmpeg

CMD ["python", "main.py"]
