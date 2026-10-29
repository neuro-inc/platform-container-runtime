ARG PY_VERSION=3.14.10

FROM python:${PY_VERSION}-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -c "from importlib.metadata import version; assert tuple(map(int, version('pip').split('.'))) >= (26, 2, 2)"

ENV PATH=/root/.local/bin:$PATH

WORKDIR /tmp
COPY requirements.txt /tmp/

RUN pip install --user --no-cache-dir -r requirements.txt

COPY dist /tmp/dist/
RUN pip install --user --no-cache-dir --find-links /tmp/dist platform-container-runtime \
    && rm -rf /tmp/dist

FROM python:${PY_VERSION}-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -c "from importlib.metadata import version; assert tuple(map(int, version('pip').split('.'))) >= (26, 2, 2)"

LABEL org.opencontainers.image.source="https://github.com/neuro-inc/platform-container-runtime"

# Name of your service (folder under /home)
ARG SERVICE_NAME="platform-container-runtime"

# Tell Python where the "user" site is
ENV HOME=/home/${SERVICE_NAME}
ENV PYTHONUSERBASE=/home/${SERVICE_NAME}/.local
ENV PATH=/home/${SERVICE_NAME}/.local/bin:$PATH

WORKDIR /home/${SERVICE_NAME}

# Copy everything from the builder’s user‐site into your service’s user‐site
COPY --from=builder /root/.local /home/${SERVICE_NAME}/.local

ENV NP_PORT=8080

EXPOSE $NP_PORT


ENTRYPOINT [ "platform-container-runtime" ]
