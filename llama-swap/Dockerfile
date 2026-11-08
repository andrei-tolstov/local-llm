FROM alpine:3.20

RUN apk add --no-cache docker-cli curl bash ca-certificates jq

ARG LS_VER=270
RUN curl -fsSL "https://github.com/mostlygeek/llama-swap/releases/download/v${LS_VER}/llama-swap_${LS_VER}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/local/bin llama-swap && \
    chmod +x /usr/local/bin/llama-swap

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
