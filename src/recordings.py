"""录音 URI 抽象层（#70）。

把「录音存在哪」从 **FS 容器内绝对路径** 升级为**可迁移 URI**，让本地盘与对象存储
共用同一套寻址，上云时前端 / CDR 表结构 / 端点契约零改动。

    <scheme>://<authority>/<path>

    本地阶段：  local://<node_uuid>/<uuid>.wav      authority = FS 节点标识
    上云阶段：  cos://<bucket>/<prefix>/<uuid>.wav  authority = 存储桶
    存量数据：  /usr/local/freeswitch/.../x.wav    裸路径 = 隐式 local://（legacy）

设计要点（详见 设计方案-录音URI抽象-v1.md）：
- 为什么 authority 放「节点」而不是用 `file://`：多节点下才分得清录音落在哪台机器的盘上，
  且 `node_uuid`（`fs_node.uuid` / `NODE_UUID` / `cdr.fs_node_uuid`）现成就有，不需要新表新列。
- 上云后 authority 从「节点」自然变成「bucket」，**URI 结构不变** → 解析层/端点/前端都不用动。
- 本模块是**纯函数、无 DB 依赖**（根路径由调用方传入），便于单测与在多处复用。

⚠️ `cdr.record_path` 列名保留（语义从 path 升格为 uri），**零 migration**；
   老数据是裸绝对路径，读取时按隐式 local:// 兼容（见 §7 兼容性矩阵）。
"""
import os
from urllib.parse import urlsplit

SCHEME_LOCAL = "local"
SCHEME_COS = "cos"

# 默认的容器内录音根（compose 把宿主 ./data/recordings 挂到这里）。
# 单机部署改这一个值即可；多机/自定义路径由调用方传 root 覆盖。
DEFAULT_ROOT = "/recordings"


def is_uri(stored) -> bool:
    """判据用 `://` 而不是「是否以字母开头加冒号」——避免把裸路径误判成 scheme。"""
    return isinstance(stored, str) and "://" in stored


def to_uri(stored, node_uuid=None, root=None) -> str | None:
    """把库里的值规范化为 URI（幂等：已是 URI 则原样返回）。

    裸路径（含存量的 FS 容器绝对路径）→ `local://<node_uuid>/<basename>`。
    只取 basename 是刻意的：宿主布局是 `/<root>/<node_uuid>/<file>`，
    节点目录与 URI 的 authority 段一一对应，URI 里不必再重复节点名。
    """
    if not stored:
        return None
    s = str(stored).strip()
    if not s:
        return None
    if is_uri(s):
        return s
    name = s.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not name:
        return None
    node = (node_uuid or "").strip()
    return "%s://%s/%s" % (SCHEME_LOCAL, node, name) if node else "%s:///%s" % (SCHEME_LOCAL, name)


def parse(stored) -> dict:
    """拆解 URI / 裸路径。

    → {scheme, authority, name, uri, legacy}
      scheme    : 'local' | 'cos' | ''（裸路径 = legacy）
      authority : local → node_uuid；cos → bucket；legacy → ''（由调用方按 CDR.fs_node_uuid 补）
      name      : 文件名
      uri       : 规范化后的 URI（legacy 时也给出，authority 可能为空）
    """
    out = {"scheme": "", "authority": "", "name": "", "uri": None, "legacy": False}
    if not stored:
        return out
    s = str(stored).strip()
    if not s:
        return out
    if not is_uri(s):
        out["legacy"] = True
        out["name"] = s.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        out["uri"] = to_uri(s)
        return out
    p = urlsplit(s)
    out["scheme"] = (p.scheme or "").lower()
    out["authority"] = p.netloc or ""
    out["name"] = (p.path or "").rstrip("/").rsplit("/", 1)[-1]
    out["uri"] = s
    return out


def resolve(stored, root=DEFAULT_ROOT, node_uuid=None, local_node=None) -> dict:
    """把库里的值解析成**本节点视角**的可读路径，供 API/后台任务使用。

    → {scheme, authority, name, uri, legacy, path, remote}
      node_uuid  : 录音**归属**节点 —— URI 取 authority；legacy 裸路径由调用方传
                   `cdr.fs_node_uuid`（存量数据没有 authority 段，只能靠 CDR 侧查）
      local_node : **本进程**所在节点，用于判 remote；不传时取 node_uuid（单节点场景等价）
      path       : 本机可读的绝对路径（scheme=local/legacy 时给出；cos 为 None）
      remote     : True = 录音**归属**别的节点。

    ⚠️ `remote=True` 不等于「读不到」：本方案的录音目录是**共享挂载**（FS 与网关、
       多节点同挂 ./data/recordings），所以别的节点的文件在这里也可能真实可达。
       调用方应以 `os.path.isfile(path)` 为准；remote 只用来在**读不到时**把
       404 升级成更有信息量的 409（见 app.cdr_recording）。
    """
    info = parse(stored)
    scheme = info["scheme"]
    owner = info["authority"]
    if info["legacy"]:
        # 升级前的存量数据是 FS 容器内绝对路径，没有 authority 段，按调用方给的归属节点补齐
        owner = (node_uuid or "").strip()
        info["authority"] = owner
        info["uri"] = to_uri(stored, owner)
    here = local_node if local_node is not None else node_uuid
    # remote 只对「落在某节点本地盘」的形态有意义；cos:// 在对象存储里，与节点无关。
    info["remote"] = bool(scheme in ("", SCHEME_LOCAL) and owner and here and owner != here)
    path = None
    if scheme in ("", SCHEME_LOCAL) and info["name"]:
        r = root or DEFAULT_ROOT
        path = os.path.join(r, owner, info["name"]) if owner else os.path.join(r, info["name"])
    info["path"] = path
    return info


def stat_local(stored, root=DEFAULT_ROOT, node_uuid=None, local_node=None) -> dict:
    """本地文件状态。→ {exists, size, path, remote}（不存在或非本节点时 exists=False）。"""
    info = resolve(stored, root=root, node_uuid=node_uuid, local_node=local_node)
    out = {"exists": False, "size": 0, "path": info["path"], "remote": info["remote"]}
    p = info["path"]
    if p:
        try:
            st = os.stat(p)
            out["exists"] = True
            out["size"] = int(st.st_size)
        except OSError:
            pass
    return out


def public_url(stored, record_cfg=None) -> str | None:
    """scheme=cos → 对象存储**签名直链**；local/legacy → None。

    ⚠️ 上云阶段才实现（需要 COS SDK 与签名逻辑，密钥只走环境变量）。
    本地阶段恒返回 None，调用方据此走「本地流式回源」分支 —— 契约在此先固化，
    上云时只改本函数 + 配置，**端点与前端不动**。
    """
    return None
