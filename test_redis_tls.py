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

def try_conn():
    kwargs = {
        "host": host,
        "port": port,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "decode_responses": True,
    }
    if pwd:
        kwargs["password"] = pwd
    print(f"Trying plain connection to {host}:{port} (password={'set' if pwd else 'none'})...")
    try:
        r = redis.Redis(**kwargs)
        pong = r.ping()
        print("PONG =>", pong)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

try_conn()
