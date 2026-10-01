# Job intelligence agent, containerised.
#
# The agent runs unattended on a schedule and needs three things kept out of
# the image: API credentials, the user's CV and company list, and the SQLite
# database, which holds a real job search. Credentials arrive at runtime as
# environment variables; the rest is mounted from the host. Nothing personal is
# copied into a layer, because a layer keeps what it was given even if a later
# step deletes it.
#
# Paths follow core/config.py, which resolves everything relative to core/:
# the database is core/data/job_digest.db, the company list is
# core/config/companies.json and the CV file is core/master-cv.yaml.
#
# Build:
#   docker build -t job-agent .
#
# Run, with secrets from a local env file and personal data kept on the host:
#   docker run --rm --env-file core/.env -e TZ=CET-1CEST,M3.5.0,M10.5.0/3 \
#     -v "$(pwd)/core/data:/app/core/data" \
#     -v "$(pwd)/core/config/companies.json:/app/core/config/companies.json:ro" \
#     -v "$(pwd)/core/master-cv.yaml:/app/core/master-cv.yaml:ro" \
#     job-agent
#
# Pinned to 3.12 rather than latest, so a new Python release cannot change the
# behaviour of a scheduled run without anyone touching the code.
FROM python:3.12-slim

# No .pyc files, and unbuffered output so docker logs shows progress live
# rather than in one burst when the run ends.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before source, so editing a Python file does not invalidate the
# cached layer that installs them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a normal user, not root. The uid is fixed so a volume written by the
# container stays readable on the host.
RUN useradd --create-home --uid 1000 agent \
 && mkdir -p /app/core/data \
 && chown -R agent:agent /app
USER agent

# The database belongs to the host, not the image.
VOLUME ["/app/core/data"]

# The same two steps as the scheduled task on the host: search and store, then
# send the Telegram digest. The send runs even if the search fails, as it does
# on the host, so jobs stored by an earlier run still go out.
CMD ["sh", "-c", "python job_search_smart.py; python telegram_sender.py"]
