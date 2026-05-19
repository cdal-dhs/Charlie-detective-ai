FROM detective-agent:base

WORKDIR /app

# Copier UNIQUEMENT le code applicatif (les deps lourdes sont déjà dans l'image base)
COPY pyproject.toml .
COPY app ./app
COPY scripts ./scripts
COPY deploy ./deploy

# Install rapide (les gros packages sont déjà là)
RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "app.main"]
