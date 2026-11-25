FROM python:3.12.7-slim

ARG APP_VERSION=dev

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    APP_VERSION=$APP_VERSION

WORKDIR /app
COPY app.py .

# Numeric non-root UID so Kubernetes can enforce runAsNonRoot. OpenShift overrides it with a
# random UID, which also works because the app writes nothing to disk.
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
