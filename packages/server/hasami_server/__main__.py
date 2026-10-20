"""python -m hasami_server  ->  http://127.0.0.1:8420 (HASAMI_HOST / HASAMI_PORT to change)."""

from .app import serve


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
