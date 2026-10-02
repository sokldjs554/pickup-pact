# syntax=docker/dockerfile:1
# Single-host rehearsal image only: Patroni from PyPI on the pinned PostgreSQL 17.11 image.
# It runs several "hosts" as containers on one machine and is never independent-host HA evidence.
# An optional build secret "build_ca" adds a TLS-intercepting proxy CA for pip; nothing is kept.
FROM python:3.12-slim-bookworm AS py
RUN --mount=type=secret,id=build_ca,required=false \
    cat /etc/ssl/certs/ca-certificates.crt $( [ -f /run/secrets/build_ca ] && echo /run/secrets/build_ca ) > /tmp/ca.pem && \
    pip install -q --no-cache-dir --cert /tmp/ca.pem --prefix=/opt/patroni 'patroni[etcd3]==4.0.4' 'psycopg[binary]==3.2.3' && \
    rm /tmp/ca.pem

FROM postgres:17.11-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652
COPY --from=py /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=py /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=py /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/
COPY --from=py /usr/lib/x86_64-linux-gnu/libffi.so.8 /usr/lib/x86_64-linux-gnu/libsqlite3.so.0 /usr/lib/x86_64-linux-gnu/
COPY --from=py /opt/patroni /opt/patroni
ENV PYTHONPATH=/opt/patroni/lib/python3.12/site-packages \
    PATH=/opt/patroni/bin:/usr/lib/postgresql/17/bin:$PATH \
    LD_LIBRARY_PATH=/usr/local/lib
RUN sed -i '1s|.*|#!/usr/local/bin/python3.12|' /opt/patroni/bin/patroni /opt/patroni/bin/patronictl && \
    python3.12 -c "import ssl, ctypes, psycopg, patroni, etcd" && patroni --version
USER postgres
ENTRYPOINT ["patroni"]
