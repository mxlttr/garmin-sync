FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY sync.py ./
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001:10001
ENV PYTHONUNBUFFERED=1 HOME=/data STATE_DB=/data/sync.sqlite3
CMD ["python", "sync.py", "serve"]
