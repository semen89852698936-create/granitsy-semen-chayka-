FROM python:3.12-alpine
WORKDIR /app
COPY . .
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python","server.py"]
