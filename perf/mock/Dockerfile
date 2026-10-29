FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mock_openai.py .
ENV WORKERS=4 PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn mock_openai:app --host 0.0.0.0 --port $PORT --workers $WORKERS --no-access-log --loop uvloop --http httptools --timeout-keep-alive 120 --backlog 4096"]
