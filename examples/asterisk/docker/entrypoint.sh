#!/bin/sh

set -eu

: "${VOXBENCH_SIP_PASSWORD:=voxbench-6001-local-only}"
: "${VOXBENCH_AMI_PASSWORD:=voxbench-ami-local-only}"
: "${VOXBENCH_AUDIOSOCKET_HOST:=host.docker.internal}"
: "${VOXBENCH_AUDIOSOCKET_PORT:=9019}"

case "$VOXBENCH_SIP_PASSWORD" in
  *[!A-Za-z0-9._-]* | "")
    echo "VOXBENCH_SIP_PASSWORD may contain only letters, digits, dot, underscore, and dash." >&2
    exit 64
    ;;
esac

case "$VOXBENCH_AMI_PASSWORD" in
  *[!A-Za-z0-9._-]* | "")
    echo "VOXBENCH_AMI_PASSWORD may contain only letters, digits, dot, underscore, and dash." >&2
    exit 64
    ;;
esac

case "$VOXBENCH_AUDIOSOCKET_HOST" in
  *[!A-Za-z0-9._:-]* | "")
    echo "VOXBENCH_AUDIOSOCKET_HOST contains unsupported characters." >&2
    exit 64
    ;;
esac

case "$VOXBENCH_AUDIOSOCKET_PORT" in
  *[!0-9]* | "")
    echo "VOXBENCH_AUDIOSOCKET_PORT must be an integer." >&2
    exit 64
    ;;
esac

if [ "$VOXBENCH_AUDIOSOCKET_PORT" -lt 1 ] || [ "$VOXBENCH_AUDIOSOCKET_PORT" -gt 65535 ]; then
  echo "VOXBENCH_AUDIOSOCKET_PORT must be between 1 and 65535." >&2
  exit 64
fi

sed \
  "s|REPLACE_WITH_LOCAL_SECRET|$VOXBENCH_SIP_PASSWORD|g" \
  /etc/asterisk/pjsip.conf.template \
  >/etc/asterisk/pjsip.conf
sed \
  -e "s|REPLACE_WITH_AUDIOSOCKET_HOST|$VOXBENCH_AUDIOSOCKET_HOST|g" \
  -e "s|REPLACE_WITH_AUDIOSOCKET_PORT|$VOXBENCH_AUDIOSOCKET_PORT|g" \
  /etc/asterisk/extensions.conf.template \
  >/etc/asterisk/extensions.conf
sed \
  "s|REPLACE_WITH_AMI_SECRET|$VOXBENCH_AMI_PASSWORD|g" \
  /etc/asterisk/manager.conf.template \
  >/etc/asterisk/manager.conf

chown asterisk:asterisk \
  /etc/asterisk/pjsip.conf \
  /etc/asterisk/extensions.conf \
  /etc/asterisk/manager.conf
chmod 0640 \
  /etc/asterisk/pjsip.conf \
  /etc/asterisk/extensions.conf \
  /etc/asterisk/manager.conf

exec asterisk -f -U asterisk -G asterisk -vvv
