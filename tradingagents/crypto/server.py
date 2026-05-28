from __future__ import annotations

import os

import uvicorn


def main():
    host = os.getenv("CRYPTO_API_HOST", "127.0.0.1")
    port = int(os.getenv("CRYPTO_API_PORT", "8000"))
    uvicorn.run("tradingagents.crypto.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
