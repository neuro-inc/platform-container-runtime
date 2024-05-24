FROM python:3.9.19-slim-bullseye AS installer

ENV PATH=/root/.local/bin:$PATH

# Copy to tmp folder to don't pollute home dir
RUN mkdir -p /tmp/dist
COPY dist /tmp/dist

RUN ls /tmp/dist
RUN pip install --user --find-links /tmp/dist platform-container-runtime

FROM python:3.9.9-slim-bullseye as service

LABEL org.opencontainers.image.source = "https://github.com/neuro-inc/platform-container-runtime"

WORKDIR /app

COPY --from=installer /root/.local/ /root/.local/

ENV PATH=/root/.local/bin:$PATH
ENV NP_PORT=8080

EXPOSE $NP_PORT

CMD platform-container-runtime
