from __future__ import annotations

import os

from waitress import serve

from . import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("WG_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WG_WEB_PORT", "8080"))
    serve(app, host=host, port=port, threads=4, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
