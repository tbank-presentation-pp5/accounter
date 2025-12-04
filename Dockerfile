FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gdown
COPY . .
RUN gdown "https://drive.google.com/uc?id=1ro9ZHmi9eUE_qSvP2XVxNP9sil6A9B1K" -O /app/auth.db
RUN ls -la /app/auth.db && echo "Database file downloaded successfully"
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]