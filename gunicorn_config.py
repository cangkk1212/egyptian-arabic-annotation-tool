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
max_requests = 5000
max_requests_jitter = 500
