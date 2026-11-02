FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIAOXU_DATA=/data PIAOXU_CONFIG=/config TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY docker-entrypoint.py ./docker-entrypoint.py
RUN useradd --create-home --uid 10001 piaoxu && mkdir -p /data /config && chown piaoxu:piaoxu /data /config
EXPOSE 8000
ENTRYPOINT ["python", "/app/docker-entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
