# Gunicorn 生产环境配置
# 使用方法: gunicorn -c gunicorn_config.py server:app

import os

# 绑定地址和端口
bind = "0.0.0.0:8080"

# 工作进程数（推荐: CPU核心数 * 2 + 1）
workers = 4

# 每个 worker 的线程数
threads = 4

# 工作模式
worker_class = "gthread"

# 超时时间（音频请求可能较长）
timeout = 300

# 日志
accesslog = "-"          # stdout
errorlog = "-"           # stderr
loglevel = "info"

# 进程名
proc_name = "audio-annotator"

# 优雅重启
graceful_timeout = 10

# 关闭 max_requests 自动回收：音频流会产生大量 Range 请求，worker 周期重启
# 会中断长音频流、制造短暂请求失败窗口（表现为 audio load failed / save failed / ERROR 1033）
max_requests = 0
max_requests_jitter = 0
