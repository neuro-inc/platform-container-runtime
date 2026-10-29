ARG PY_VERSION=3.14.10

FROM python:${PY_VERSION}-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install --no-cache-dir --upgrade "certifi==2026.10.23" \
    && python -c "import pip, certifi; assert pip.__version__ == '26.2.2'; assert certifi.__version__ == '2026.10.23'"

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
    && python -m pip install --no-cache-dir --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install --no-cache-dir --upgrade "certifi==2026.10.23" \
    && python -c "import pip, certifi; assert pip.__version__ == '26.2.2'; assert certifi.__version__ == '2026.10.23'"

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
