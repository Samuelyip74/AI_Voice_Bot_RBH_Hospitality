FROM andrius/asterisk:latest

USER root

COPY agi/requirements.txt /tmp/agi-requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip ca-certificates gettext-base \
    && python3 -m pip install --break-system-packages --no-cache-dir -r /tmp/agi-requirements.txt \
    && rm -rf /var/lib/apt/lists/* /tmp/agi-requirements.txt

USER asterisk
