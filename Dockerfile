# SIP-SWITCH Python 网关镜像
#
# 关键点：python-esl 不在 PyPI，无法 pip 安装。
# 这里用多阶段构建，从 FreeSWITCH 镜像里取出 ESL 的 Python 绑定（ESL.py + _ESL*.so）。
# 若所用 FS 镜像内没有 ESL 绑定，构建会打印 WARN 并继续（ESL 功能降级，见 docs/docker.md 排错）。

ARG FS_IMAGE=sip-switch-fs:1.11.2

# ---------- stage 1：从 FS 镜像收集 ESL python 绑定 ----------
FROM ${FS_IMAGE} AS fsesl
RUN mkdir -p /esl-out \
 && (find / -xdev -name "ESL.py"   2>/dev/null | head -n 1 | xargs -r -I{} cp {} /esl-out/) \
 && (find / -xdev -name "_ESL*.so" 2>/dev/null | head -n 1 | xargs -r -I{} cp {} /esl-out/) \
 && (test -f /esl-out/ESL.py || echo "ESL bindings NOT FOUND in FS image" > /esl-out/MISSING)

# ---------- stage 2：运行时 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_CONFIG=/app/config/config_settings.yaml

WORKDIR /app

COPY requirements.txt ./
# 国内网络下 PyPI 默认不可达，默认走阿里云镜像；可 --build-arg PIP_INDEX_URL=... 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

# 安装 ESL python 绑定（来自 stage 1）
COPY --from=fsesl /esl-out /tmp/esl-out
RUN set -e; \
    SP=$(python -c "import site; print(site.getsitepackages()[0])"); \
    if [ -f /tmp/esl-out/ESL.py ]; then \
      cp /tmp/esl-out/ESL.py "$SP"/; \
      cp /tmp/esl-out/_ESL*.so "$SP"/ 2>/dev/null || true; \
      python -c "import ESL; print('[build] ESL ok')"; \
    else \
      echo "[build] WARN: ESL python 绑定未在 FS 镜像中找到，ESL 功能将不可用"; \
      cat /tmp/esl-out/MISSING 2>/dev/null || true; \
    fi

COPY src/ ./src/
COPY conftest.py pytest.ini ./

# 落地网关 XML 写入共享卷（一期方案 A）；由环境变量指定路径
ENV FS_SIP_PROFILES_EXTERNAL=/fs-profiles

EXPOSE 8000
CMD ["python", "src/main.py"]
