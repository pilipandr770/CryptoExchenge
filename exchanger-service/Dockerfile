FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chmod +x docker/entrypoint.sh

EXPOSE 5000

ENV FLASK_APP=wsgi.py

CMD ["./docker/entrypoint.sh"]
