# syntax=docker/dockerfile:1
# Single-host rehearsal runtime for the Pickup Pact roles. The source tree is mounted
# read-only at /opt/pickup-pact; only the Python dependencies are baked in.
FROM python:3.12-slim-bookworm
COPY requirements-ha.txt requirements-workbench.txt /tmp/requirements/
RUN --mount=type=secret,id=build_ca,required=false \
    cat /etc/ssl/certs/ca-certificates.crt $( [ -f /run/secrets/build_ca ] && echo /run/secrets/build_ca ) > /tmp/ca.pem && \
    pip install -q --no-cache-dir --cert /tmp/ca.pem -r /tmp/requirements/requirements-ha.txt && \
    rm -rf /tmp/ca.pem /tmp/requirements && useradd --uid 10001 --home-dir /tmp --no-create-home pickup
WORKDIR /opt/pickup-pact
ENV PYTHONPATH=/opt/pickup-pact:/opt/pickup-pact/services/reconciler PYTHONDONTWRITEBYTECODE=1
