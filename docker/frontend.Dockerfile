# Stage 1: Build React + Vite frontend
FROM node:22-alpine AS frontend-builder

WORKDIR /app

COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY frontend/ ./
RUN npm run build

# Stage 2: Serve static SPA and proxy API/SSE/SymSrv via Nginx
FROM nginx:1.27-alpine

COPY --from=frontend-builder /app/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
