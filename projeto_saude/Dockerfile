ARG BUILD_FROM=python:3.12-alpine
FROM python:3.12-alpine
WORKDIR /app
COPY server.py /app/server.py
COPY www /app/www
CMD ["python3", "-u", "/app/server.py"]
