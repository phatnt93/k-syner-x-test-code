"""Minimal TCP proxy used by failure scenario F4 ("PostgreSQL down").

    python scripts/tcp_proxy.py --listen 15432 --target localhost:5432

CDMS connects through it; killing this process cuts every connection and refuses new ones, exactly what CDMS
sees when the database dies — without stopping the real PostgreSQL, which other databases on the server use.
"""

import argparse
import asyncio
import contextlib
import sys


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--target", required=True, help="host:port")
    args = parser.parse_args()
    host, port = args.target.rsplit(":", 1)

    async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(host, int(port))
        except OSError:
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, server_writer), pipe(server_reader, client_writer))

    server = await asyncio.start_server(handle, "127.0.0.1", args.listen)
    print(f"proxy 127.0.0.1:{args.listen} -> {args.target}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    sys.exit(0)
