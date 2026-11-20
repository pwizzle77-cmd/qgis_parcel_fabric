# -*- coding: utf-8 -*-
"""A tiny client for a local Ollama server (standard library only).

Only loopback HTTP(S) endpoints are accepted. Proxies and redirects are disabled.
"""

import json
import urllib.error
import urllib.request
import urllib.parse

DEFAULT_URL = "http://localhost:11434"


class OllamaError(RuntimeError):
    pass


def _url(base, path):
    """Keep deed contents on this computer, including when a URL was saved earlier."""
    try:
        parts = urllib.parse.urlsplit(base.strip() or DEFAULT_URL)
        if (parts.scheme not in ("http", "https") or parts.hostname not in ("localhost", "127.0.0.1", "::1")
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or parts.path not in ("", "/")):
            raise ValueError()
        port = parts.port
    except ValueError:
        raise OllamaError("Read Deed uses a local Ollama only. "
                          "Use http://localhost:11434 or a loopback IP address.") from None
    host = "[::1]" if parts.hostname == "::1" else "127.0.0.1"
    return "%s://%s%s%s" % (parts.scheme, host, ":%d" % port if port is not None else "", path)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise OllamaError("AI service redirects are disabled to protect deed contents and credentials.")


def _open(req, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(req, timeout=timeout)


def _post(url, path, body, timeout):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_url(url, path), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with _open(req, timeout) as resp:
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
        with _open(req, timeout) as resp:
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
    if "cloud" in model.lower().rsplit(":", 1)[-1].split("-"):
        raise OllamaError("Choose a downloaded local model, not an Ollama cloud model.")
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
