"""沙盒（阶段 4.2，decisions/0021）：模型写的代码在加固的一次性容器里执行。

只有 sandbox 服务（app.py）持有 Docker socket，对 worker 只开放窄接口；worker 用 client.py 调用。
"""
