FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Forma shell de propósito, para ${PORT} ser lido na hora de rodar.
# A Railway escolhe a porta por variável: com 8000 fixo, o contêiner sobe e nunca recebe tráfego.
CMD uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000}
