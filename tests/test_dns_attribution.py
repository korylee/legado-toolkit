# -*- coding: utf-8 -*-
"""DNS 失败归因：域名真注销（该删）vs 本机解析被污染（该翻墙）。

**全部离线**：报文解析喂构造好的字节，判定逻辑喂假的解析结果。真去查公共
DNS 的用例在 CI 上必然不稳定（网络一变结论就变），而这里要守的是判定规则本身。

为什么值得单独守：这两个结论的后果**相反**——判 `DEAD` 用户会去删源，判 `GFW`
用户会去开代理。判错一个方向都是「推荐用户删掉还能用的源」。
"""

from __future__ import annotations

import unittest

from core import dns_check
from core.checker import classify_transport_error
from core.models import Health


def _reply(qid: int, rcode: int, answers: list) -> bytes:
    """造一个应答报文：answers 是 [(type, rdata_bytes), ...]。"""
    header = (qid.to_bytes(2, "big") + (0x8180 | rcode).to_bytes(2, "big")
              + (1).to_bytes(2, "big") + len(answers).to_bytes(2, "big")
              + b"\x00\x00" + b"\x00\x00")
    question = b"\x01a\x03com\x00" + b"\x00\x01" + b"\x00\x01"
    body = b""
    for rtype, rdata in answers:
        body += b"\xc0\x0c" + rtype.to_bytes(2, "big") + b"\x00\x01" \
                + (60).to_bytes(4, "big") + len(rdata).to_bytes(2, "big") + rdata
    return header + question + body


class ReplyParsingTests(unittest.TestCase):
    """报文解析：**只认自己那个 id**。被注入/串包的应答必须挡掉。"""

    def test_a_record_is_read_back(self) -> None:
        kind, ip = dns_check.parse_reply(_reply(0x1234, 0, [(1, bytes([1, 2, 3, 4]))]), 0x1234)
        self.assertEqual((kind, ip), ("answer", "1.2.3.4"))

    def test_nxdomain_is_recognized(self) -> None:
        kind, _ = dns_check.parse_reply(_reply(0x1234, 3, []), 0x1234)
        self.assertEqual(kind, "nxdomain")

    def test_reply_with_a_different_id_is_ignored(self) -> None:
        """污染注入的典型形态：随机 id 的伪造应答。落到库里就是"能解析"的假证据。"""
        kind, _ = dns_check.parse_reply(_reply(0x9999, 0, [(1, bytes([1, 2, 3, 4]))]), 0x1234)
        self.assertEqual(kind, "error")

    def test_other_rcodes_are_not_answers(self) -> None:
        self.assertEqual(dns_check.parse_reply(_reply(1, 2, []), 1)[0], "error")
        self.assertEqual(dns_check.parse_reply(b"\x00" * 5, 1)[0], "error")

    def test_cname_then_a_is_followed(self) -> None:
        """CNAME 在前、A 在后也要能取到 A。"""
        cname = b"\x01a\x03com\x00"
        data = _reply(7, 0, [(5, cname), (1, bytes([9, 8, 7, 6]))])
        self.assertEqual(dns_check.parse_reply(data, 7), ("answer", "9.8.7.6"))


class BuildQueryTests(unittest.TestCase):
    def test_idn_host_is_sent_as_punycode(self) -> None:
        """非 ASCII 域名要转 punycode。

        不转的话 `encode("ascii")` 抛异常 → 被当成"解析器连不上" → 这类源永远停在
        「待复检」，而原因与 DNS 无关（库里实测有这种源）。
        """
        packet = dns_check._build_query("飞速中文.com", 1)
        self.assertIn(b"xn--", packet, "应当出现 punycode 标签（xn--）")
        self.assertIn(b"\x03com\x00", packet)
        self.assertNotIn("飞速".encode("utf-8"), packet, "中文原样发出去是发不出去的")

    def test_garbage_host_does_not_raise(self) -> None:
        """源里那些根本不是域名的 url（如「🌐绅士漫画」）不能把查询器带崩——
        它会被解析器拒掉，落到 unknown/待复检。"""
        packet = dns_check._build_query("🌐绅士漫画", 1)
        self.assertTrue(packet.startswith(b"\x00\x01"), "首部里的 id 字段还在")


class ProbeVerdictTests(unittest.TestCase):
    """判定规则。**关键是"只有一个来源说话时不下结论"**。"""

    def setUp(self) -> None:
        self._orig = dns_check._query

    def tearDown(self) -> None:
        dns_check._query = self._orig

    def _stub(self, *kinds):
        queue = list(kinds)

        async def fake(_server, _name):
            return queue.pop(0), ""      # _query 的契约是 (kind, ip)
        dns_check._query = fake

    def _probe(self, *kinds):
        self._stub(*kinds)
        import asyncio
        return asyncio.run(dns_check.probe("example.com"))

    def test_any_answer_means_polluted(self) -> None:
        """有一个来源能解析出来 → 域名是存在的，是本机解析不到。"""
        verdict, note = self._probe("answer", "nxdomain")
        self.assertEqual(verdict, dns_check.POLLUTED)
        self.assertIn("能解析到", note)     # note 要能说明"我们看到了什么"

    def test_only_both_saying_nxdomain_means_gone(self) -> None:
        """**判死必须两个独立来源都同意**。

        境内解析器对封锁域名常常直接回 NXDOMAIN（那是策略应答，不是"域名不存在"），
        只凭它判死 = 推荐用户删掉需要翻墙的源。
        """
        self.assertEqual(self._probe("nxdomain", "nxdomain")[0], dns_check.GONE)
        self.assertEqual(self._probe("nxdomain", "unreachable")[0], dns_check.UNKNOWN,
                         "只有一个来源说不存在时不能判死")
        self.assertEqual(self._probe("nxdomain", "error")[0], dns_check.UNKNOWN)

    def test_unreachable_resolvers_never_produce_a_verdict(self) -> None:
        """本机断网时公共 DNS 也连不上——这时只能待复检，不能判死。

        这是断网场景的守门用例：整机离线时全量校验会给每个源都报 DNS 失败，
        判死就等于一次断网删掉半个书库。
        """
        self.assertEqual(self._probe("unreachable", "unreachable")[0], dns_check.UNKNOWN)

    def test_empty_host_is_unknown(self) -> None:
        import asyncio
        verdict, _note = asyncio.run(dns_check.probe(""))
        self.assertEqual(verdict, dns_check.UNKNOWN)


class DnsHealthMappingTests(unittest.TestCase):
    """判定结果 → 健康态。**这里错了上层全错**，所以单独钉住映射。"""

    def test_dns_transport_error_stays_pending_by_default(self) -> None:
        """传输层分类里 DNS 仍然只是「待复检」——归因要外部视角才能做。"""
        self.assertEqual(classify_transport_error("dns"), Health.PENDING)

    def test_certificate_error_has_its_own_bucket(self) -> None:
        """证书错误单独一档：它是这批源里**唯一我们自己能处理**的一类
        （站点是通的，关掉证书校验就能用），混进「待验证」等于把可操作的信息丢了。"""
        self.assertEqual(classify_transport_error("cert"), Health.CERT)
        self.assertNotEqual(classify_transport_error("cert"), Health.PENDING)


class ClassifyDnsTests(unittest.TestCase):
    """`AsyncChecker._classify_dns`：判定结果 → (健康态, 文案)，以及按域名缓存。

    判定对了、映射错了，用户看到的还是错的结论（比如"域名已注销"配上 `GFW` 这个
    健康态）——所以这一层单独测。
    """

    def setUp(self) -> None:
        from core.checker import AsyncChecker
        from core.models import build_record
        self.ck = AsyncChecker(concurrency=1, use_store=False)
        self.rec = build_record({"bookSourceUrl": "https://a.example/",
                                 "bookSourceName": "A"}, 0)
        self.calls = []
        self._orig = dns_check.probe

    def tearDown(self) -> None:
        dns_check.probe = self._orig
        self.ck.close()

    def _stub(self, verdict: str):
        async def fake(host):
            self.calls.append(host)
            return verdict, "（说明）"
        dns_check.probe = fake

    def _run(self):
        import asyncio
        return asyncio.run(self.ck._classify_dns(self.rec, "https://a.example/"))

    def test_polluted_maps_to_gfw(self) -> None:
        self._stub(dns_check.POLLUTED)
        health, error = self._run()
        self.assertEqual(health, Health.GFW)
        self.assertIn("代理", error, "文案要告诉用户下一步怎么做")

    def test_gone_maps_to_dead(self) -> None:
        self._stub(dns_check.GONE)
        health, error = self._run()
        self.assertEqual(health, Health.DEAD)
        self.assertIn("删除", error)

    def test_unknown_maps_to_pending(self) -> None:
        self._stub(dns_check.UNKNOWN)
        health, error = self._run()
        self.assertEqual(health, Health.PENDING, "验不出来就维持待复检，不能判死")
        self.assertIn("待复检", error)

    def test_verdict_is_cached_per_host(self) -> None:
        """同一主机的源成批出现（实测 35% 的主机有两条以上源），
        **每条都去查一遍公共 DNS 是白费**——而且那是对外发的查询。"""
        self._stub(dns_check.POLLUTED)
        self._run()
        self._run()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
