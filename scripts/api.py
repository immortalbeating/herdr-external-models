"""Small Responses / Chat Completions adapter. No tool loop or provider fallback."""
import base64
import json
import mimetypes
import os
from pathlib import Path
import tomllib
from urllib import error, parse, request


def provider(spec):
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = Path(spec.get("config", home / "config.toml"))
    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    entry = config.get("model_providers", {}).get(spec["provider"])
    if not isinstance(entry, dict):
        raise ValueError("Provider is not defined in model_providers")
    env = entry.get("env_key")
    if not env or not os.environ.get(env):
        raise ValueError("Provider requires a populated env_key environment variable")
    base = str(entry.get("base_url", ""))
    base = os.environ.get(base, base).rstrip("/")
    url = parse.urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query:
        raise ValueError("Provider requires an HTTP(S) base_url without embedded credentials/query")
    wire = entry.get("wire_api", "responses").replace("-", "_")
    if wire not in ("responses", "chat", "chat_completions"):
        raise ValueError("Only Responses / Chat Completions wire formats are supported")
    return base, wire, os.environ[env]


def image_url(raw):
    path = Path(raw)
    mime = mimetypes.guess_type(path.name)[0]
    if not path.is_file() or not mime or not mime.startswith("image/"):
        raise ValueError("Image must be an existing image file")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Image exceeds 20 MiB")
    return "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def payload(spec, prompt, wire):
    images = [image_url(p) for p in spec.get("images", [])]
    result = {"model": spec["model"], "stream": False}
    if wire == "responses":
        content = [{"type": "input_text", "text": prompt}]
        content += [{"type": "input_image", "image_url": p} for p in images]
        result["input"] = [{"role": "user", "content": content}]
    else:
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": p}} for p in images]
        result["messages"] = [{"role": "user", "content": content}]
    return result


def decode(data, wire):
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Provider returned an error or non-object response")
    if wire == "responses":
        if data.get("status") not in (None, "completed"):
            raise ValueError("Response is not completed: " + str(data.get("status")))
        text = data.get("output_text") or "\n".join(
            part.get("text", "")
            for item in data.get("output", []) if isinstance(item, dict)
            for part in item.get("content", []) if isinstance(part, dict)
            if part.get("type") in ("output_text", "text")
        )
    else:
        choice = (data.get("choices") or [{}])[0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("Chat response did not finish normally: " + str(choice.get("finish_reason")))
        text = choice.get("message", {}).get("content")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Provider returned no final text")
    return text.strip()


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Provider redirect refused; configure the final endpoint explicitly")


def call(spec, prompt):
    base, wire, key = provider(spec)
    suffix = "/responses" if wire == "responses" else "/chat/completions"
    url = base if base.endswith(suffix) else base + suffix
    req = request.Request(url, data=json.dumps(payload(spec, prompt, wire)).encode("utf-8"),
                          headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with request.build_opener(NoRedirect).open(req, timeout=spec.get("timeout", 180)) as response:
            data = json.load(response)
    except error.HTTPError as exc:
        # Raw provider error bodies may echo credentials or private prompts.
        code = exc.code
        exc.close()
        raise ValueError(f"Provider HTTP {code}; no automatic retry") from None
    return decode(data, wire), {"model_reported": data.get("model"),
                               "response_id": data.get("id"), "usage": data.get("usage", {})}
