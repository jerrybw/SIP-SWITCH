# 自建 FreeSWITCH 运行时镜像
#
# 为什么需要自建：
#   - Docker Hub 上不存在 signalwire/freeswitch 运行时镜像（拉取返回 denied / RST）。
#   - signalwire/freeswitch-public-base 只是「编译基座」，内含编译依赖与工具链，
#     但不包含 freeswitch 本体（which freeswitch 为空、无 /usr/local/freeswitch）。
#   - SignalWire 官方 apt 源（freeswitch.signalwire.com）返回 401，需账号 PAT，无法匿名安装。
# 因此：用官方基座 + github 上的 v1.11.2 tag 从源码编译，产出与生产环境同版本的运行时镜像。
#
# 构建：
#   docker build -f deploy/fs-image/Dockerfile -t sip-switch-fs:1.11.2 .
# 可选参数：
#   --build-arg FS_REF=v1.11.2   源码 tag
#   --build-arg JOBS=8           编译并发数

ARG FS_BASE=signalwire/freeswitch-public-base:latest

# ---------- stage 1：编译 ----------
FROM ${FS_BASE} AS builder

ARG FS_REF=v1.11.2
ARG JOBS=8

# FS 1.10+ 的 libs/（libks、signalwire-client-c、spandsp、sofia-sip 等）是 git submodule，
# 必须递归拉取，否则 bootstrap / configure 会失败。
RUN git clone --depth 1 --branch ${FS_REF} --recurse-submodules --shallow-submodules \
      https://github.com/signalwire/freeswitch.git /usr/src/freeswitch

WORKDIR /usr/src/freeswitch

RUN ./bootstrap.sh -j
# configure 参数与生产环境保持一致（见生产 /usr/src/freeswitch-1.11.2/config.log）
RUN ./configure --prefix=/usr/local/freeswitch --disable-core-pgsql
RUN make -j${JOBS}
RUN make install

# ESL 的 Python 绑定不在 PyPI，只能从源码构建；网关镜像会在多阶段构建时来这里取
RUN cd libs/esl \
 && (make py3mod || echo "WARN: py3mod 构建失败，ESL Python 绑定将不可用")

# ---------- stage 2：运行时 ----------
FROM ${FS_BASE}

COPY --from=builder /usr/local/freeswitch /usr/local/freeswitch
# ESL Python 绑定（ESL.py + _ESL*.so），供网关镜像 stage 1 用 find 捞取
COPY --from=builder /usr/src/freeswitch/libs/esl/python3/ /esl-python/

RUN echo /usr/local/freeswitch/lib > /etc/ld.so.conf.d/freeswitch.conf && ldconfig

# prefix 布局下的默认配置目录：/usr/local/freeswitch/etc/freeswitch
ENV FS_CONF_DIR=/usr/local/freeswitch/etc/freeswitch
ENV PATH=/usr/local/freeswitch/bin:/usr/local/freeswitch/sbin:${PATH}
# 兼容旧约定：若基座镜像里没有 /etc/freeswitch，建软链指向真实配置目录
RUN if [ ! -e /etc/freeswitch ]; then ln -s /usr/local/freeswitch/etc/freeswitch /etc/freeswitch; fi

EXPOSE 5060/udp 5080/udp 8021

CMD ["freeswitch", "-c", "-nonat"]
