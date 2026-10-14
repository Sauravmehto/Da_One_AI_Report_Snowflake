# ---- dev ----
FROM node:22-alpine AS dev
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install
COPY . .
EXPOSE 5173
CMD ["pnpm", "dev", "--host"]

# ---- build ----
FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
# Vite reads VITE_* at BUILD time and inlines the values into the bundle, so
# this cannot be a runtime container variable — setting it in compose
# `environment:` does nothing, and the Report Builder link stays hidden with no
# error to explain why. It has to be an ARG, promoted to ENV for `pnpm build`.
ARG VITE_SHOW_REPORT_BUILDER=false
ENV VITE_SHOW_REPORT_BUILDER=$VITE_SHOW_REPORT_BUILDER
RUN pnpm build

# ---- prod (nginx) ----
FROM nginx:alpine AS prod
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
