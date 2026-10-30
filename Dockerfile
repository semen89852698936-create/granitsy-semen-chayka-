# Portable Flask deployment for a Russian-accessible Docker host (e.g. Timeweb Cloud).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app
RUN useradd --system --uid 10001 --create-home course && chown -R course:course /app
USER course
EXPOSE 8080
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 75 --access-logfile - --error-logfile - app:app"]
