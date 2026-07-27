FROM alpine:3.22

RUN apk add --no-cache asterisk asterisk-sample-config

COPY --chown=asterisk:asterisk config/ /etc/asterisk/
COPY entrypoint.sh /usr/local/bin/voxbench-asterisk-entrypoint

RUN chmod 0755 /usr/local/bin/voxbench-asterisk-entrypoint

EXPOSE 5060/udp
EXPOSE 5038/tcp
EXPOSE 10000-10099/udp

ENTRYPOINT ["/usr/local/bin/voxbench-asterisk-entrypoint"]
