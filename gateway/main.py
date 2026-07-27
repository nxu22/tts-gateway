"""FastAPI entry point: ``POST /synthesize`` (buffered) and ``WS /stream`` (streaming).

Application code is bound by the same rule as the router: no vendor names. It talks
only to `gateway.router` and `gateway.interface`.
"""
