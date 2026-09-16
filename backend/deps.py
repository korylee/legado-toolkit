# -*- coding: utf-8 -*-
# FastAPI 依赖注入：每个请求一个 Store 连接。
# 注意：SQLite 单写者，Store 已配 busy_timeout=5000，长任务要短事务提交。
#
# **这条连接一定会跨线程**：sync 依赖与 sync 端点各自向 anyio 线程池要线程，
# 不保证是同一个 worker（实测并发时 `sqlite3` 会抛 ProgrammingError 直接 500）。
# 所以 Store 的连接是带 `check_same_thread=False` 开的——别把它去掉，也别在这条
# 路径上依赖「同一个线程」这个前提。
from core.store import Store


def get_store():
    st = Store()
    try:
        yield st
    finally:
        st.close()
