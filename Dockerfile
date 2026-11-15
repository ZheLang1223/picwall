FROM docker.1ms.run/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
# 非 root 运行（2026-10-11 风险分析 F9）：进程被攻破时不再是容器 root。
# data/ 是 bind mount，macOS Docker Desktop 的文件共享层按宿主用户放行读写。
RUN useradd --create-home --uid 1000 pw
COPY app/ app/
COPY tools/runtime.py tools/backup.py tools/maintenance.py tools/pair.py tools/
# 架构图谱（PWALL 设置 → 开发管理 /atlas 读它；源文件在仓库 docs/，镜像里保持同相对路径）
COPY docs/project_atlas.html docs/project_atlas.html
USER pw
EXPOSE 8080 8082
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["python", "-m", "app.serve"]
