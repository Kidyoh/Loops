FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY loops ./loops
RUN pip install --no-cache-dir .

COPY alembic.ini ./
