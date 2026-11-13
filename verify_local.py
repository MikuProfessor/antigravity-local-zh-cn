#!/usr/bin/env python3
"""Check the local SillyTavern -> CLIProxyAPI route using synthetic messages."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
ST = "http://127.0.0.1:8000"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    integration = json.loads((ROOT / "runtime/sillytavern-integration.json").read_text())
    session = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    csrf = json.load(session.open(ST + "/csrf-token", timeout=10))["token"]
    body = {
        "chat_completion_source": "custom",
        "custom_url": integration["api_base"],
        "secret_id": integration["secret_id"],
        "custom_include_headers": "",
        "custom_include_body": "",
        "custom_exclude_body": "",
        "custom_prompt_post_processing": "",
        "model": args.model,
        "messages": [
            {"role": "system", "content": "你是一位简洁的中文助手。每次回复必须以[AGY]开头，不要添加解释。"},
            {"role": "user", "content": "请记住，这间茶馆的名字叫月光渡口。"},
            {"role": "assistant", "content": "记住了，茶馆叫月光渡口。"},
            {"role": "user", "content": "茶馆叫什么名字？回复名字后加上 END_OF_TEST。"},
        ],
        "stream": args.stream,
        "max_tokens": 1024,
        "temperature": 0.3,
        "top_p": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
    }
    request = urllib.request.Request(
        ST + "/api/backends/chat-completions/generate",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
    )
    start = time.monotonic()
    result = {"model_requested": args.model, "stream": args.stream, "passed": False}
    content = ""
    finish_reason = None
    done = False
    try:
        with session.open(request, timeout=120) as response:
            result["http"] = response.status
            if args.stream:
                chunks = 0
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    event = json.loads(data)
                    if event.get("error"):
                        raise RuntimeError("Upstream returned an error event")
                    chunks += 1
                    for choice in event.get("choices", []):
                        content += choice.get("delta", {}).get("content") or ""
                        finish_reason = choice.get("finish_reason") or finish_reason
                result["chunks"] = chunks
                result["sse_done"] = done
            else:
                data = json.load(response)
                if data.get("error") or not data.get("choices"):
                    raise RuntimeError("No valid completion: " + str(data.get("error", "missing choices")))
                choice = data["choices"][0]
                content = choice.get("message", {}).get("content") or ""
                finish_reason = choice.get("finish_reason")
        result.update(content=content, finish_reason=finish_reason)
        result["passed"] = (
            content.lstrip().startswith("[AGY]")
            and "月光渡口" in content
            and "END_OF_TEST" in content
            and finish_reason == "stop"
            and (done or not args.stream)
        )
    except urllib.error.HTTPError as exc:
        result.update(http=exc.code, error="HTTP request failed")
    except Exception as exc:
        result["error"] = str(exc)
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    output = ROOT / "runtime" / ("verify-" + args.model + ("-stream" if args.stream else "") + ".json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
