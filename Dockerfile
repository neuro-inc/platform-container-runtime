FROM python:3.8-slim-buster AS installer

ARG PIP_EXTRA_INDEX_URL

# Separate step for requirements to speed up docker builds
COPY platform_container_runtime.egg-info/requires.txt requires.txt
RUN python -c 'from pkg_resources import Distribution, PathMetadata;\
dist = Distribution(metadata=PathMetadata(".", "."));\
print("\n".join(str(r) for r in dist.requires()));\
' > requirements.txt
RUN pip install -U pip && pip install --user -r requirements.txt

ARG DIST_FILENAME

# Install service itself
COPY dist/${DIST_FILENAME} ${DIST_FILENAME}
RUN pip install --user $DIST_FILENAME

FROM python:3.8-slim-buster as service

WORKDIR /app

COPY --from=installer /root/.local/ /root/.local/

ENV PATH=/root/.local/bin:$PATH
ENV NP_PORT=8080

EXPOSE $NP_PORT

CMD platform-container-runtime
