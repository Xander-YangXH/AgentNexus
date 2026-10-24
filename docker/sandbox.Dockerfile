# 沙盒运行镜像（decisions/0021）：模型写的代码在这个镜像的一次性容器里执行。
# sandbox 服务创建容器时再加上断网、只读根文件系统、nobody 用户、资源上限等限制（见 sandbox/runner.py）。
# 只在构建时安装依赖、版本锁定：运行时没有网络，也不允许 pip install。
FROM python:3.12-slim

# 中文字体（阶段 7.2）：没有它时图表里的中文是方框，模型会花好几轮工具调用去找字体、换字体（实测把 8 轮用完，
# 图画好了却没来得及交付）。文泉驿微米黑：Debian 官方源，安装后约 5MB；matplotlib 通过 MATPLOTLIBRC 默认用它
RUN apt-get update && apt-get install -y --no-install-recommends fonts-wqy-microhei && rm -rf /var/lib/apt/lists/*
COPY docker/sandbox/matplotlibrc /etc/matplotlibrc

COPY docker/sandbox/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# matplotlib 的配置 / 字体缓存目录放在可写的 tmpfs 上。第一版在构建时预生成到 /opt（只读），
# 结果每次运行都在 stderr 打警告“不可写、改用临时目录”；实测 slim 镜像字体很少，每次现建缓存加画图只要约 0.3 秒，不值得预生成
ENV MPLCONFIGDIR=/tmp/matplotlib MPLBACKEND=Agg MATPLOTLIBRC=/etc/matplotlibrc

WORKDIR /workspace
USER 65534:65534
CMD ["python", "-c", "print('sandbox runtime ok')"]
