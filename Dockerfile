FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PORT=8080
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 8 --timeout 300"]
