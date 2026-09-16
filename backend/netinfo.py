# -*- coding: utf-8 -*-
"""本机地址探测。**只有这一份**。

两处要用：`GET /api/net`（前端拿它拼「手机能访问的地址」、生成导出二维码）与
`python -m backend` 的启动日志。分头写必然漂成两个答案，而这两处的用途正是
同一个——「手机该连哪个地址」，报错一个就等于报错两个。

**只依赖标准库**：`backend/__main__.py` 要在**不导入 app** 的前提下用它
（导入 app 会把 fastapi 与全部路由拖进启动进程，还会把 app 里那些启动提示
打印两遍）。
"""
from __future__ import annotations

import os
import socket
from typing import List


def local_ips() -> List[str]:
    """本机的局域网 IP 列表，**出口网卡排第一**。

    第一条用 UDP socket 试探：连 8.8.8.8 但**不发包**，只让内核按路由表选出
    出口网卡——这比枚举所有网卡再挑更接近「手机能不能连上」的真实答案。
    后面再补上主机名解析出来的其余地址，因为第一顺位未必是用户想要的那张
    网卡（装了 VMware / Hyper-V / WSL 的机器上，虚拟网卡常常排前面）。

    探测失败就返回空列表，调用方自己决定怎么报——这里不编造地址。
    """
    ips: List[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def hostname() -> str:
    return socket.gethostname()


def port() -> int:
    """监听端口。`LEGADO_PORT` 是**唯一**来源。

    `backend/__main__.py` 拿到 `--port` 后会回写这个环境变量，所以命令行传的
    端口在这里也读得到——否则会出现「服务在 9000 上跑，报给手机的却是 8787」，
    而二维码扫出来连不上、没有任何线索。
    """
    return int(os.getenv("LEGADO_PORT", "8787"))
