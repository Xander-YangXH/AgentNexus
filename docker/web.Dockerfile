# 构建上下文是仓库根目录（见 docker-compose.yml 的 web.build）

# ---- 构建阶段：类型检查 + 打包 ----
FROM node:22-alpine AS build
WORKDIR /web
# 先只拷依赖清单：改业务代码时依赖层缓存不失效
COPY web/package.json web/package-lock.json ./
# npm ci：严格按锁文件安装，保证与本地一致；--ignore-scripts：不执行依赖包的安装脚本（供应链安全，本项目也不需要）
RUN npm ci --no-fund --no-audit --ignore-scripts
COPY web/ ./
RUN npm run build

# ---- 运行阶段：只带静态文件和 Nginx，不含 Node 与 node_modules ----
FROM nginx:1.28-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
