FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TRUST_PROXY=1 MODEL_BACKEND=custom DATABASE_PATH=/app/data/planetx.db PORT=8000
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /app/data && chown -R app /app/data
USER app
VOLUME /app/data
EXPOSE 8000
# worker واحد عمدًا: الـ rate limiter في الذاكرة وSQLite بيكتب من عملية واحدة
CMD gunicorn app:app --workers 1 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT
