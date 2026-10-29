FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000
ENV DB_PATH=/data/habits.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY habit-bot-new.py .
COPY index.html .

RUN mkdir -p /data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn habit-bot-new:app --host 0.0.0.0 --port ${PORT}"]