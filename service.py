"""接口进程：/health 健康探针 + 材料隔离后端。

环境变量：
- PORT                 监听端口，默认 8080
- DB_PATH              SQLite 路径，默认 data/isolation.db
- ISOLATION_TOKENS     JSON 对象，配置四类角色的 Bearer 令牌
- ISOLATION_SIGNING_KEY 复核签名密钥

本地开发未配置时使用下面的开发令牌，切勿用于生产。
"""

import json
import os
from http.server import ThreadingHTTPServer

from app import IsolationApp, make_handler
from store import Store

DEFAULT_TOKENS = {
    "intake": "dev-intake-token",
    "custodian": "dev-custodian-token",
    "reviewer": "dev-reviewer-token",
    "investigator": "dev-investigator-token",
}

_app = None


def load_tokens():
    raw = os.environ.get("ISOLATION_TOKENS")
    if raw:
        return json.loads(raw)
    return dict(DEFAULT_TOKENS)


def get_app():
    global _app
    if _app is None:
        db_path = os.environ.get("DB_PATH", os.path.join("data", "isolation.db"))
        _app = IsolationApp(
            Store(db_path),
            load_tokens(),
            os.environ.get("ISOLATION_SIGNING_KEY", "dev-signing-key"),
        )
    return _app


Handler = make_handler(get_app)

if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "--seed" in argv:
        from seed import seed

        seeded = seed(get_app().store, argv[argv.index("--seed") + 1])
        print("已导入样例原件:", ", ".join(seeded))
    ThreadingHTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler
    ).serve_forever()
