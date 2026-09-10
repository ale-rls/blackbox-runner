# Keep this version aligned with local development and bump it deliberately.
FROM alpine:3.20

ARG PB_VERSION=0.39.11
ARG PB_ARCH=amd64

RUN apk add --no-cache ca-certificates curl unzip

WORKDIR /pb

RUN curl -sL \
      "https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}/pocketbase_${PB_VERSION}_linux_${PB_ARCH}.zip" \
      -o pocketbase.zip \
    && unzip pocketbase.zip pocketbase \
    && rm pocketbase.zip \
    && chmod +x pocketbase

# PocketBase applies committed migrations automatically when it starts.
COPY pb_migrations ./pb_migrations

# Runtime state is never baked into the image. Mount a persistent volume here.
VOLUME ["/pb/pb_data"]
EXPOSE 8090

CMD ["/pb/pocketbase", "serve", "--http=0.0.0.0:8090", "--dir=/pb/pb_data", "--migrationsDir=/pb/pb_migrations"]
