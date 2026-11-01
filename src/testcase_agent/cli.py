import argparse

import uvicorn

from .config import Settings


def main():
    parser = argparse.ArgumentParser(description="Run the Spec to Test prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not Settings().app_token:
        parser.error("Set APP_TOKEN before binding beyond localhost")
    uvicorn.run("testcase_agent.app:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
