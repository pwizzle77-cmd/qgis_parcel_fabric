# -*- coding: utf-8 -*-
"""A tiny client for a local Ollama server (standard library only).

Nothing here leaves the computer unless the Ollama URL in settings points
somewhere else.
"""

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:11434"


class OllamaError(RuntimeError):
    pass


def _url(base, path):
    """http(s) only - never file:// or other schemes urllib would accept."""
    full = base.strip().rstrip("/") + path
    if not full.lower().startswith(("http://", "https://")):
        raise OllamaError("The Ollama URL must start with http:// or https:// (got %r)." % base)
    return full


def _post(url, path, body, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_url(url, path), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme checked in _url
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise OllamaError("Ollama said %s: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise OllamaError("Can't reach Ollama at %s (%s). Is it running?" % (url, e.reason))


def list_models(url=DEFAULT_URL, timeout=5):
    """Names of the models pulled into Ollama, e.g. ['qwen2.5vl:7b', ...]."""
    req = urllib.request.Request(_url(url, "/api/tags"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme checked in _url
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise OllamaError("Can't reach Ollama at %s (%s). Is it running?" % (url, getattr(e, "reason", e)))
    return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


def pick(models, *prefixes):
    """The first model whose name starts with one of ``prefixes``."""
    for p in prefixes:
        for m in models:
            if m.split(":")[0] == p or m.startswith(p):
                return m
    return models[0] if models else ""


def chat(url, model, prompt, images_b64=None, schema=None, timeout=600, temperature=0.0, system=None):
    """One request; returns the reply text.

    ``images_b64``: base64 PNG/JPEG strings for vision models.
    ``schema``: a JSON schema - Ollama then constrains the reply to it.
    """
    msg = {"role": "user", "content": prompt}
    if images_b64:
        msg["images"] = list(images_b64)
    messages = ([{"role": "system", "content": system}] if system else []) + [msg]
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"temperature": temperature}}
    if schema is not None:
        body["format"] = schema
    data = _post(url, "/api/chat", body, timeout)
    try:
        return data["message"]["content"]
    except (KeyError, TypeError):
        raise OllamaError("Unexpected reply from Ollama: %s" % str(data)[:200])


def chat_json(url, model, prompt, schema, images_b64=None, timeout=600, system=None):
    """``chat`` and parse the JSON reply."""
    text = chat(url, model, prompt, images_b64, schema, timeout, system=system)
    try:
        return json.loads(text)
    except ValueError:
        # some models wrap JSON in a code fence
        t = text.strip().strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        try:
            return json.loads(t)
        except ValueError:
            raise OllamaError("The model didn't return valid JSON: %s" % text[:200])
