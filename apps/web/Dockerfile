FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# NEXT_PUBLIC_* are inlined at BUILD time, so the API base must be a build arg
# (runtime env_file is too late for client bundles).
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
# `proxy.ts` only redirects to /login when this is "1". The API is the real
# gate (it 401s regardless); this flag exists so a dev build with AUTH_ENABLED=0
# doesn't bounce you to a login screen with no password to type.
ARG NEXT_PUBLIC_AUTH_ENABLED=0
ENV NEXT_PUBLIC_AUTH_ENABLED=$NEXT_PUBLIC_AUTH_ENABLED
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
