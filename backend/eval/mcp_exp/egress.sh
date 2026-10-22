#!/bin/sh
# 网络拓扑：client 只在 internal 网络（不通外网）；squid 同时在 internal 和普通网络
set -e
docker network create --internal anx_exp_internal >/dev/null
docker network create anx_exp_outside >/dev/null
docker run -d --name anx_exp_squid --network anx_exp_outside -p 127.0.0.1:53128:3128 \
  -v "$PWD/squid.conf:/etc/squid/squid.conf:ro" ubuntu/squid:6.6-24.04_edge >/dev/null
docker network connect anx_exp_internal anx_exp_squid
sleep 4
# 前置检查：代理必须在运行。第一版没检查，squid 启动即退出，所有探测都因“找不到代理”失败，看起来像“都拦下了”
if [ "$(docker inspect -f '{{.State.Running}}' anx_exp_squid 2>/dev/null)" != "true" ]; then
  echo "squid 没有在运行："; docker logs anx_exp_squid 2>&1 | tail -5; exit 1
fi
probe() {  # $1 说明 $2 URL $3 是否走代理
  printf "%-38s " "$1"
  docker run --rm --network anx_exp_internal ${3:+-e HTTP_PROXY=http://anx_exp_squid:3128 -e HTTPS_PROXY=http://anx_exp_squid:3128} \
    python:3.12-slim python -c "
import urllib.request
try:
    r = urllib.request.urlopen('$2', timeout=6); print('可达', r.status, r.geturl()[:50])
except Exception as e: print('拦下', type(e).__name__, str(e)[:70])" 2>&1 | tail -1
}
probe "直连外网（不走代理）" https://example.com
probe "经代理访问外网" https://example.com 1
probe "经代理访问宿主机上的 Qdrant" http://host.docker.internal:6333/collections 1
probe "经代理访问云元数据地址" http://169.254.169.254/latest/meta-data/ 1
probe "经代理访问 squid 自己(localhost)" http://localhost:3128/ 1
probe "外网页面重定向到内网" "http://httpbin.org/redirect-to?url=http://host.docker.internal:6333/collections" 1
