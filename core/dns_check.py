# -*- coding: utf-8 -*-
"""DNS 失败归因：域名真注销，还是本机解析被污染？

域名探测报 DNS 失败时**只有本机视角**，无法区分这两件事，所以原来一律按「待复检」
处理（`core/checker.py` 的 `classify_transport_error` 里写了这个理由）。这里补一个
**外部视角**：直接向公共解析器查一次 A 记录。

**必须直发 IP（UDP/53），不能走系统解析**——要查的恰恰是"系统解析为什么失败"，
用系统解析器去查等于没查（本机 DNS 坏了的时候连查都发不出去）。

判定只用**明确答复**，说不清就说不清：

    任一给出 A 记录        → polluted  域名存在，是本机解析不到 → 该翻墙 / 换 DNS
    两个都回 NXDOMAIN      → gone      域名确实不存在           → 该删
    其余（超时 / 不可达 / 只有一方回 NXDOMAIN） → unknown      → 维持「待复检」

**判死必须两个独立来源都同意**：境内解析器对封锁域名常常直接回 NXDOMAIN（那是策略
应答，不是"域名不存在"），只凭它就会把「该翻墙」的源判成「该删」——而判死的后果是
用户把它们删掉。反过来，境外解析器给出的可疑 IP（污染注入）只会让我们判成
「该翻墙」，方向是安全的。
"""

from __future__ import annotations

import asyncio
import random
import struct
from typing import List, Tuple

#: 交叉验证用的解析器。**国内 + 国外各一个**，理由见模块开头：只用一个来源
#: 不足以区分「策略性 NXDOMAIN」和「域名真的不存在」
QUERY_SERVERS: Tuple[Tuple[str, str], ...] = (
    ("223.5.5.5", "阿里公共DNS"),
    ("1.1.1.1", "Cloudflare"),
)

DNS_PORT = 53
#: 单个解析器的超时。**短**：这是给全量校验链路加的步骤，一次 DNS 失败最多加
#: 一个超时的时长（两个查询并发），不能拖慢整轮校验
QUERY_TIMEOUT = 2.5

#: 判定结果（三种，别再加第四种：说不清就是 unknown）
POLLUTED = "polluted"
GONE = "gone"
UNKNOWN = "unknown"

#: 无法核实时的说明文案（要拼进用户看到的那句话里，所以是中文）
_KIND_NAMES = {"answer": "能解析", "nxdomain": "说不存在",
               "unreachable": "连不上", "error": "应答异常"}


def _build_query(name: str, qid: int) -> bytes:
    """构造一个 A 记录查询报文（RD=1，单问题段）。"""
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii")
                     for p in name.split(".") if p) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)   # QTYPE=A, QCLASS=IN


def _skip_name(data: bytes, pos: int) -> int:
    """跳过域名编码（含压缩指针），返回下一个字段的位置。"""
    while pos < len(data):
        ln = data[pos]
        if ln == 0:
            return pos + 1
        if ln & 0xC0 == 0xC0:      # 压缩指针：两字节，且它一定是名字的结尾
            return pos + 2
        pos += 1 + ln
    return pos


def parse_reply(data: bytes, qid: int) -> Tuple[str, str]:
    """解析应答，返回 ``(kind, ip)``：kind ∈ {"answer", "nxdomain", "error"}。

    只认与请求 id 相符、RCODE 为 0/3 的应答——被污染或串包的应答在这里就被挡掉，
    不会变成"某个 IP"。
    """
    if len(data) < 12:
        return "error", ""
    rid, flags, qdcount, ancount = struct.unpack(">HHHH", data[:8])
    if rid != qid:
        return "error", ""
    rcode = flags & 0x000F
    if rcode == 3:
        return "nxdomain", ""
    if rcode != 0:
        return "error", ""
    pos = 12
    for _ in range(qdcount):                      # 问题段：名字 + QTYPE/QCLASS
        pos = _skip_name(data, pos) + 4
    for _ in range(ancount):
        pos = _skip_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        if rtype == 1 and rdlen == 4 and pos + 4 <= len(data):
            return "answer", ".".join(str(b) for b in data[pos:pos + 4])
        pos += rdlen
    return "error", ""


class _DatagramProtocol(asyncio.DatagramProtocol):
    """收第一个应答就结束（DNS 一问一答）。"""

    def __init__(self) -> None:
        self.reply: asyncio.Future = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, _addr) -> None:
        if not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc: Exception) -> None:      # pragma: no cover
        if not self.reply.done():
            self.reply.set_exception(exc)


async def _query(server: str, name: str) -> Tuple[str, str]:
    """向 ``server`` 查 ``name`` 的 A 记录。连不通/超时统一回 ("unreachable", "")。"""
    loop = asyncio.get_running_loop()
    qid = random.randrange(1, 0xFFFF)     # 随机 id：应答 id 不符的直接当无效包丢掉
    try:
        transport, proto = await loop.create_datagram_endpoint(
            _DatagramProtocol, remote_addr=(server, DNS_PORT))
    except Exception:
        return "unreachable", ""
    try:
        transport.sendto(_build_query(name, qid))
        data = await asyncio.wait_for(proto.reply, QUERY_TIMEOUT)
    except Exception:
        return "unreachable", ""
    finally:
        transport.close()
    return parse_reply(data, qid)


async def probe(host: str) -> Tuple[str, str]:
    """交叉验证一个域名，返回 ``(verdict, note)``。

    ``note`` 是**要给用户看的一句话**（会拼进 ``record.error``），所以它说的是
    "我们观察到了什么"，不是"结论是什么"。
    """
    name = (host or "").split(":")[0].strip().strip(".")
    if not name or "/" in name:
        return UNKNOWN, "域名为空"
    results = await asyncio.gather(*[_query(ip, name) for ip, _label in QUERY_SERVERS])
    kinds: List[str] = [r[0] for r in results]
    for (kind, ip), (_srv, label) in zip(results, QUERY_SERVERS):
        if kind == "answer":
            return POLLUTED, "%s 能解析到 %s" % (label, ip)
    if all(k == "nxdomain" for k in kinds):
        return GONE, "两个公共 DNS 均应答「域名不存在」"
    return UNKNOWN, "公共 DNS 无法核实（%s）" % "、".join(
        "%s=%s" % (label, _KIND_NAMES.get(kind, kind))
        for (kind, _ip), (_srv, label) in zip(results, QUERY_SERVERS))
