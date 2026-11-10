# Frontend image.
#
# `dev` runs the Vite dev server with hot reload, which is what compose uses, so
# the host never needs Node installed. `prod` serves the built assets from nginx
# and is what deploys.

FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ── Development ──────────────────────────────────────────────────────────────
FROM deps AS dev
WORKDIR /app
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ── Build ────────────────────────────────────────────────────────────────────
FROM deps AS build
WORKDIR /app
COPY . .
ARG VITE_API_BASE_URL=/
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

# ── Production ───────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS prod
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
