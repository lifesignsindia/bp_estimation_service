#!/usr/bin/env python3
"""Small connectivity tester to try plaintext and TLS Redis connections.
Run this inside a running pod/container that has network path to your Redis.
"""
import os
import sys
try:
    import redis
except Exception as e:
    print("redis package not available:", e)
    sys.exit(2)

host = os.getenv("REDIS_HOST", "localhost")
port = int(os.getenv("REDIS_PORT", "6379"))
pwd = os.getenv("REDIS_PASSWORD") or None
ssl_env = os.getenv("REDIS_SSL", "false").lower() == "true"

def try_conn(use_ssl):
    kwargs = {
        "host": host,
        "port": port,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "decode_responses": True,
    }
    if pwd:
        kwargs["password"] = pwd
    if use_ssl:
        kwargs["ssl"] = True
        kwargs["ssl_cert_reqs"] = None
    mode = "TLS" if use_ssl else "plain"
    print(f"Trying {mode} connection to {host}:{port} (password={'set' if pwd else 'none'})...")
    try:
        r = redis.Redis(**kwargs)
        pong = r.ping()
        print("PONG (mode=", mode, ") =>", pong)
    except Exception as e:
        print(f"ERROR ({mode}): {type(e).__name__}: {e}")

if ssl_env:
    try_conn(True)
else:
    # try plaintext first, then TLS to help diagnose mismatch
    try_conn(False)
    print("--- now attempting TLS as well (diagnostic) ---")
    try_conn(True)
