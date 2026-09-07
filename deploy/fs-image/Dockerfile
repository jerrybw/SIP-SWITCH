# 自建 FreeSWITCH 运行时镜像（v1.11.2，与生产同版本）
#
# 为什么自建：
#   - Docker Hub 上不存在 signalwire/freeswitch 运行时镜像（拉取返回 denied / RST）。
#   - signalwire/freeswitch-public-base 只是「编译基座」，只有依赖与工具链，
#     不含 freeswitch 本体（which freeswitch 为空、无 /usr/local/freeswitch）。
#   - SignalWire 官方 apt 源（freeswitch.signalwire.com）返回 401，匿名装不了包。
#
# 依赖说明（已核实 configure.ac 与发布包 modules.conf）：
#   - sofia-sip-ua >= 1.13.18  必装（mod_sofia 的 SIP 栈）
#   - spandsp      >= 3.1.1    必装（DSP/传真，生产环境同样编了）
#   - libks2 / signalwire_client2 只在 mod_verto(WebRTC) / mod_signalwire 启用时才需要，
#     本镜像用不到这两个模块，已在 modules.conf 中禁用，故跳过其编译。
#   - 注意：FS v1.11.2 没有 .gitmodules，libs/ 里不含上述依赖，必须在编译 FS 前装好。
#
# 构建：
#   docker build -f deploy/fs-image/Dockerfile -t sip-switch-fs:1.11.2 .
# 可选参数：
#   --build-arg FS_REF=v1.11.2        FS 源码 tag
#   --build-arg SOFIA_REF=v1.13.18    sofia-sip tag
#   --build-arg SPANDSP_REF=v3.1.1    spandsp tag
#   --build-arg JOBS=8                编译并发数

ARG FS_BASE=signalwire/freeswitch-public-base:latest

# ---------- stage 1：编译 ----------
FROM ${FS_BASE} AS builder

ARG FS_REF=v1.11.2
ARG SOFIA_REF=v1.13.18
ARG SPANDSP_REF=v3.1.1
ARG JOBS=8

# pkg-config 需要能找到装到 /usr/local 的 .pc 文件
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib64/pkgconfig:${PKG_CONFIG_PATH}
ENV LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH}

# 预检：ESL 的 python 绑定需要 swig 与 python3-dev（缺失时尝试 apt 补装，失败仅告警）
RUN (command -v swig && command -v python3-config) \
 || (apt-get update && apt-get install -y --no-install-recommends swig python3-dev) \
 || echo "WARN: swig/python3-dev 不可用，ESL python 绑定可能构建失败"

# ---- sofia-sip ----
RUN git clone --depth 1 --branch ${SOFIA_REF} https://github.com/freeswitch/sofia-sip.git /usr/src/sofia-sip
WORKDIR /usr/src/sofia-sip
RUN ./bootstrap.sh -j && ./configure --prefix=/usr/local && make -j${JOBS} && make install && ldconfig

# ---- spandsp ----
RUN git clone --depth 1 --branch ${SPANDSP_REF} https://github.com/freeswitch/spandsp.git /usr/src/spandsp
WORKDIR /usr/src/spandsp
RUN ./bootstrap.sh -j && ./configure --prefix=/usr/local && make -j${JOBS} && make install && ldconfig

# ---- freeswitch ----
RUN git clone --depth 1 --branch ${FS_REF} https://github.com/signalwire/freeswitch.git /usr/src/freeswitch
WORKDIR /usr/src/freeswitch

# 模块开关：启用 mod_xml_curl（发布包里默认是注释掉的，不打开就编不出 xml_curl 模块）；
# 禁用 mod_verto / mod_signalwire 以跳过 libks2 与 signalwire_client2 依赖。
# 用宽容正则改写，避免不同版本 modules.conf 的路径/注释形态差异导致漏改。
RUN sed -i -E "/mod_xml_curl/s@^#@@" modules.conf \
 && sed -i -E "/mod_verto/s@^#?@#@" modules.conf \
 && sed -i -E "/mod_signalwire/s@^#?@#@" modules.conf \
 && echo "--- modules.conf 关键项 ---" \
 && grep -nE "mod_xml_curl|mod_event_socket|mod_verto|mod_signalwire" modules.conf \
 && echo "--- 断言：mod_verto / mod_signalwire 必须已禁用，否则 configure 会因缺 libks2 报错 ---" \
 && ! grep -qE "^[^#]*mod_(verto|signalwire)" modules.conf

RUN ./bootstrap.sh -j
# configure 参数与生产环境保持一致（见生产 /usr/src/freeswitch-1.11.2/config.log）
RUN ./configure --prefix=/usr/local/freeswitch --disable-core-pgsql
RUN make -j${JOBS}
RUN make install

# ESL 的 Python 绑定不在 PyPI，只能从源码构建；网关镜像会在多阶段构建时来这里取
RUN cd libs/esl \
 && (make py3mod || echo "WARN: py3mod 构建失败，ESL Python 绑定将不可用") \
 && ls -l python3/ 2>/dev/null | head

# ---------- stage 2：运行时 ----------
FROM ${FS_BASE}

COPY --from=builder /usr/local/freeswitch /usr/local/freeswitch
COPY --from=builder /usr/local/lib /usr/local/lib
# ESL Python 绑定（ESL.py + _ESL*.so），供网关镜像 stage 1 用 find 捞取
COPY --from=builder /usr/src/freeswitch/libs/esl/python3/ /esl-python/

RUN echo /usr/local/lib > /etc/ld.so.conf.d/usr-local.conf \
 && echo /usr/local/freeswitch/lib > /etc/ld.so.conf.d/freeswitch.conf \
 && ldconfig

# 源码安装（prefix=/usr/local/freeswitch）的配置与模块目录
ENV FS_CONF_DIR=/usr/local/freeswitch/etc/freeswitch
ENV FS_MOD_DIR=/usr/local/freeswitch/lib/freeswitch/mod
ENV PATH=/usr/local/freeswitch/bin:/usr/local/freeswitch/sbin:${PATH}
# 兼容旧约定：若基座镜像里没有 /etc/freeswitch，建软链指向真实配置目录
RUN if [ ! -e /etc/freeswitch ]; then ln -s /usr/local/freeswitch/etc/freeswitch /etc/freeswitch; fi

EXPOSE 5060/udp 5080/udp 8021

CMD ["freeswitch", "-c", "-nonat"]
