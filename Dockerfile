FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        cdparanoia \
        eject \
        flac \
        libdiscid0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY cd_ripper/ ./cd_ripper/

# -o allows GID 11 even if the base image already uses it (Debian reserves it for 'fax').
RUN groupadd -g 11 -o hostcdrom && \
    useradd -u 1000 -G hostcdrom -m -s /sbin/nologin ripper && \
    chown -R ripper /app

USER 1000

# pyudev reads disc events from the host udev socket.
# Run with: docker run --device /dev/sr0 -v /run/udev:/run/udev <image>
CMD ["python", "-m", "cd_ripper.main"]
