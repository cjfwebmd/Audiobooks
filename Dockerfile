FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt
COPY --chown=user . .
# deixa a voz Cadu do Piper já baixada na imagem (se falhar, ela é baixada no primeiro uso)
RUN python -c "import motor; motor._piper_arquivos('Cadu')" || echo "voz Cadu será baixada no primeiro uso"
EXPOSE 10000
CMD ["sh", "-c", "uvicorn servidor:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips '*'"]
