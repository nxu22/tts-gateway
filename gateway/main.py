"""FastAPI 入口:``POST /synthesize``(整段)与 ``WS /stream``(流式)。

业务层同样不许出现 vendor 名字 —— 只跟 `gateway.router` 和 `gateway.interface` 打交道。
"""
