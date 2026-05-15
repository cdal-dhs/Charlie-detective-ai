FROM python:3.11-slim

WORKDIR /app

# Empêcher Python de générer des .pyc et bufferiser stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml .
COPY app ./app
COPY scripts ./scripts
COPY deploy ./deploy

RUN pip install --no-cache-dir -e .

EXPOSE 8080 8765

CMD ["python", "-m", "app.main"]
