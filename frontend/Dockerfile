FROM node:20-alpine AS build
ARG APP_BUILD_SHA=dev
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .
RUN npm run build && printf '{"build":"%s"}\n' "$APP_BUILD_SHA" > /app/dist/release.json

# Minimal image — publish hashed assets before atomically replacing index.html.
# Keep older hashed assets so tabs/pages cached during a rollout never reference
# a bundle that the deployment has already deleted.
FROM alpine:3.19
COPY --from=build /app/dist /dist
CMD mkdir -p /srv/frontend/assets && \
    cp -r /dist/assets/. /srv/frontend/assets/ && \
    find /dist -maxdepth 1 -type f ! -name index.html -exec cp {} /srv/frontend/ \; && \
    cp /dist/index.html /srv/frontend/index.html.next && \
    mv /srv/frontend/index.html.next /srv/frontend/index.html
