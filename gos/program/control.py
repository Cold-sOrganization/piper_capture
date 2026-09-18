#!/usr/bin/env python3
"""Local control endpoint, normally invoked by the receiver through SSH."""
import json
import sys
import urllib.error
import urllib.request


def main():
    body = sys.stdin.buffer.read()
    request = urllib.request.Request('http://127.0.0.1:8765/', body, {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            print(response.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode())
        sys.exit(1)


if __name__ == '__main__':
    main()
