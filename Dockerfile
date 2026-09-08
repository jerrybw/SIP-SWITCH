# SIP-SWITCH Python 网关镜像
#
# ESL 采用纯 Python 标准库 socket 实现（src/fs_esl_socket.py），
# 不再依赖 FreeSWITCH 原生 python-esl 绑定（ABI 不匹配），
# 故移除原先从 FS 镜像多阶段拷贝 ESL.py / _ESL*.so 的逻辑。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_CONFIG=/app/config/config_settings.yaml

WORKDIR /app

COPY requirements.txt ./
# 国内网络下 PyPI 默认不可达，默认走阿里云镜像；可 --build-arg PIP_INDEX_URL=... 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY src/ ./src/
COPY conftest.py pytest.ini ./

# 落地网关 XML 写入共享卷（一期方案 A）；由环境变量指定路径
ENV FS_SIP_PROFILES_EXTERNAL=/fs-profiles

EXPOSE 8000
CMD ["python", "src/main.py"]
