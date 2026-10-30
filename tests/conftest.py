import os
import shutil
import socket
import subprocess
import time

import pytest

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6390/0")


@pytest.fixture(scope="session")
def redis_url():
    import redis
    try:
        redis.Redis.from_url(REDIS_URL).ping()
        return REDIS_URL
    except Exception:
        if not shutil.which("redis-server"):
            pytest.skip("no redis available")
        subprocess.Popen(["redis-server", "--port", "6390", "--save", ""], stdout=subprocess.DEVNULL)
        time.sleep(0.5)
        return "redis://localhost:6390/0"
