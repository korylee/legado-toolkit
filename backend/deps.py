# -*- coding: utf-8 -*-
# FastAPI 依赖注入：每个请求一个 Store 连接。
# 注意：SQLite 单写者，Store 已配 busy_timeout=5000，长任务要短事务提交。
from core.store import Store


def get_store():
    st = Store()
    try:
        yield st
    finally:
        st.close()
