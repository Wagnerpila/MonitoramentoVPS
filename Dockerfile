FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html ./

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1

CMD ["python", "app.py"]
