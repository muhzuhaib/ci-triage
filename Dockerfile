# Two stages, so the image that runs in production is not the image that built
# it. The builder produces a wheel and the runtime installs that wheel, which
# means the container is verified the same way a stranger installing from PyPI
# would be, rather than by running out of a source tree that happens to be on
# the disk. It also keeps hatchling and the build cache out of the final layers.
FROM python:3.13-slim AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir build==1.2.2.post1

# LICENSE and README.md are copied because the packaging metadata references
# them; without either, the build fails at the point where it reads pyproject.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


FROM python:3.13-slim AS runtime

# psycopg[binary] ships its own libpq, so no apt packages are needed here. That
# is the reason the binary wheel is the declared dependency rather than psycopg
# built from source: it keeps this stage to the base image plus Python code.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /dist /dist
# The extras are applied to the wheel itself, so the versions installed are the
# ones pyproject declares. Naming them again here would be a second source of
# truth that drifts.
RUN wheel="$(ls /dist/*.whl)" \
    && python -m pip install --no-cache-dir "${wheel}[service,postgres]" \
    && rm -rf /dist

# Not root. The receiver reads a socket that the public internet can reach, and
# a process that never needs to write to the filesystem has no reason to be able
# to write to the image either.
RUN useradd --create-home --uid 10001 triage
USER triage
WORKDIR /home/triage

EXPOSE 8000

# The health check asks the application, which asks the database. urllib is used
# rather than curl because curl is not in the slim base image and the point of
# the check is not worth an apt layer.
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=6 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# --factory, because the application is built from the environment at startup:
# a missing webhook secret has to kill the process rather than be discovered on
# the first delivery. See ci_triage.app.Settings.from_env.
CMD ["python", "-m", "uvicorn", "ci_triage.app:build", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
