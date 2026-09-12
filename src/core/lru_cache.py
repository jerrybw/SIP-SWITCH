"""有界 LRU 缓存（OrderedDict 实现，纯标准库）。

替换各处「超量即全清」的裸 dict 缓存（如 T-205 _FAILOVER_CACHE / directory
_sip_call_ctx）：全清在高负载下会把**在途呼叫的上下文**连坐丢弃 —— failover
重入请求拿不到缓存文档会重新走选路，与首呼决策不一致；改 LRU 只淘汰最久未用
条目，在途上下文（刚写入/刚命中）天然保留。

线程模型：与原裸 dict 相同——CPython dict/OrderedDict 单操作原子，
move_to_end + popitem 两个操作间可能交错，最坏结果是某条被重复淘汰一次，
不会 KeyError（popitem 用 item 语法则不会），可接受；不引入锁。
"""
from collections import OrderedDict
from typing import Any


class LRUCache:
    """固定容量的 LRU 缓存。容量 ≤0 视为 1（防呆）。"""

    def __init__(self, capacity: int):
        self._cap = max(1, int(capacity))
        self._d: "OrderedDict[Any, Any]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key) -> bool:
        return key in self._d

    def get(self, key, default=None):
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return default

    def put(self, key, value) -> None:
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = value
        if len(self._d) > self._cap:
            self._d.popitem(last=False)   # 淘汰最久未用

    def clear(self) -> None:
        self._d.clear()
