FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml /build/
COPY src /build/src
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip build \
 && python -m build --wheel --outdir /dist /build

FROM python:3.12-slim
RUN groupadd -g 1030 artifactory \
 && useradd -u 1030 -g 1030 -m -s /sbin/nologin artifactory
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/*.whl \
 && rm /tmp/*.whl
RUN mkdir -p /var/airlift/state /var/airlift/spool /etc/airlift \
 && chown -R 1030:1030 /var/airlift /etc/airlift
USER 1030:1030
ENV PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO
ENTRYPOINT ["python", "-m", "artifactory_airlift"]
