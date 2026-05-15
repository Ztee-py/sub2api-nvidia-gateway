FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY server.py /app/server.py
COPY probe_upstream.py /app/probe_upstream.py

EXPOSE 8000

CMD ["python", "/app/server.py"]
