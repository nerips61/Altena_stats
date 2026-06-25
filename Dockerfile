FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_PORTAL=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY altena/ altena/
COPY templates/ templates/
COPY static/ static/
COPY config/config.json config/config.json
COPY data/ data/

EXPOSE 5070

CMD ["python", "app.py"]
