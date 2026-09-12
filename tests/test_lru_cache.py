"""LRUCache 单元测试（core/lru_cache.py，PR：缓存超量全清改 LRU 淘汰）。

覆盖：
- 容量边界：第 cap+1 个写入淘汰最久未用条目（而非全清）。
- 命中刷新：get 命中会把条目移到最新端，改变后续淘汰对象。
- 在途保护语义（本次改造的核心动机）：写入满容量后，最近写入的条目仍在 ——
  对应 T-205 failover 重入缓存，高负载下新呼叫的多腿文档不被连坐丢弃。
- put 覆盖同 key、contains、clear、防呆容量（<=0 视为 1）。
"""
import pytest

from core.lru_cache import LRUCache


def test_evicts_oldest_not_all():
    c = LRUCache(3)
    for i in range(3):
        c.put(f"k{i}", i)
    c.put("k3", 3)                     # 淘汰 k0，其余保留
    assert "k0" not in c
    assert c.get("k1") == 1 and c.get("k2") == 2 and c.get("k3") == 3
    assert len(c) == 3


def test_get_refreshes_recency():
    c = LRUCache(3)
    for i in range(3):
        c.put(f"k{i}", i)
    c.get("k0")                        # k0 变最新，淘汰对象转为 k1
    c.put("k3", 3)
    assert c.get("k0") == 0            # 幸存
    assert "k1" not in c


def test_recent_writes_survive_overflow():
    """在途保护：连写超容量后，最近的条目必须都在（旧实现此处会全部丢失）。"""
    c = LRUCache(500)
    for i in range(800):
        c.put(f"call-{i}", i)
    for i in range(300, 800):          # 最近 500 条全在
        assert c.get(f"call-{i}") == i


def test_put_overwrites_same_key():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("a", 2)
    assert c.get("a") == 2 and len(c) == 1


def test_contains_and_clear_and_default():
    c = LRUCache(2)
    c.put("a", 1)
    assert "a" in c and "b" not in c
    assert c.get("missing", "dft") == "dft"
    c.clear()
    assert len(c) == 0


def test_capacity_floor():
    assert len(LRUCache(0)) == 0
    c = LRUCache(-5)
    c.put("a", 1)
    c.put("b", 2)
    assert len(c) == 1 and "b" in c
