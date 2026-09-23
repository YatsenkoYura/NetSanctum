#!/usr/bin/env python3
import argparse
import getpass
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import httpx


def endpoint(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    if not path.endswith(suffix):
        path = f"{path}{suffix}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def print_response(label: str, response: httpx.Response) -> None:
    print(f"\n[{label}] HTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), ensure_ascii=False, indent=2)[:12000])
    except ValueError:
        print(response.text[:12000])


def post(client: httpx.Client, url: str, headers: dict[str, str], payload: dict, label: str):
    print(f"\nPOST {url}")
    print("Payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        response = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        print(f"\n[{label}] Transport error: {exc}")
        return None
    print_response(label, response)
    return response


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check authentication, chat completions, and tool calling on an OpenAI-compatible API."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL", ""),
        help="Base URL or full chat completions endpoint (or OPENAI_BASE_URL)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_MODEL", ""),
        help="Model ID (or OPENAI_MODEL)",
    )
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    if not args.base_url or not args.model:
        parser.error("--base-url and --model are required")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("API key (input hidden): ").strip()
    if not api_key:
        parser.error("API key is required via OPENAI_API_KEY or the hidden prompt")

    url = endpoint(args.base_url, "/chat/completions")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    basic_payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Reply with exactly: token works"}],
        "temperature": 0,
        "max_tokens": 20,
    }
    tool_payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Use the ping tool with value test."}],
        "temperature": 0,
        "max_tokens": 80,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Diagnostic tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }

    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        basic = post(client, url, headers, basic_payload, "basic chat")
        if basic is None or basic.status_code >= 400:
            print("\nResult: authentication, endpoint, or model check failed. Tool test skipped.")
            return 1
        tools = post(client, url, headers, tool_payload, "tool calling")

    if tools is None or tools.status_code >= 400:
        print("\nResult: token and basic chat work, but tool calling failed.")
        return 2
    print("\nResult: token, basic chat, and tool calling all work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
