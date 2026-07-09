FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY packages/netvault-server ./packages/netvault-server

RUN pip install --no-cache-dir ./packages/netvault-server

EXPOSE 8000

CMD ["uvicorn", "netvault_server.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
