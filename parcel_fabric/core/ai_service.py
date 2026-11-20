"""Profile-local AI configuration and Ollama/OpenAI-compatible requests."""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import ollama

AIError = ollama.OllamaError
PREFIX = "ParcelFabric/ai/"
PROVIDERS = {
    "ollama": ("Ollama (local)", ollama.DEFAULT_URL),
    "openai": ("OpenAI", "https://api.openai.com/v1"),
    "lmstudio": ("LM Studio", "http://localhost:1234/v1"),
    "custom": ("Custom (OpenAI-compatible)", ""),
}


def load(settings, provider=None):
    provider = provider or settings.value(PREFIX + "provider", "ollama", type=str)
    if provider not in PROVIDERS:
        provider = "ollama"
    values = {"provider": provider}
    defaults = dict(url=PROVIDERS[provider][1], ocr_model="", llm_model="", authcfg="")
    for key, default in defaults.items():
        if provider == "ollama" and key != "authcfg":
            legacy = {"url": "url", "ocr_model": "ocr_model", "llm_model": "llm_model"}[key]
            default = settings.value("ParcelFabric/ollama_" + legacy, default, type=str)
        values[key] = settings.value(PREFIX + provider + "/" + key, default, type=str)
    return values


def save(settings, values):
    validate(values)
    provider = values["provider"]
    for key in ("url", "ocr_model", "llm_model", "authcfg"):
        settings.setValue(PREFIX + provider + "/" + key, values.get(key, "").strip())
    settings.setValue(PREFIX + "provider", provider)


def endpoint(base):
    try:
        parts = urllib.parse.urlsplit(base.strip().rstrip("/"))
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment):
            raise ValueError()
        parts.port
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise AIError("Use HTTPS for AI services outside this computer.")
        if parts.hostname == "localhost":
            parts = parts._replace(netloc="127.0.0.1" + (":%d" % parts.port if parts.port else ""))
        return urllib.parse.urlunsplit(parts)
    except ValueError:
        raise AIError("Enter an HTTP(S) API base URL without credentials, query or fragment.") from None


def validate(values):
    if values.get("provider") not in PROVIDERS:
        raise AIError("Choose an AI service.")
    if values["provider"] == "ollama":
        ollama._url(values.get("url", ""), "/api/tags")
    else:
        endpoint(values.get("url", ""))


def api_key(authcfg):
    """Decrypt only on the UI thread; never store keys in a project or QgsSettings."""
    if not authcfg:
        return ""
    from qgis.core import QgsApplication, QgsAuthMethodConfig
    config = QgsAuthMethodConfig()
    manager = QgsApplication.authManager()
    if manager is None or not manager.loadAuthenticationConfig(authcfg, config, True):
        raise AIError("Unlock QGIS authentication or select a valid API credential in AI Services.")
    if config.method() != "Basic" or not config.config("password"):
        raise AIError("Use a Basic authentication configuration with the API key in its password field.")
    return config.config("password")


def client(values):
    validate(values)
    key = api_key(values.get("authcfg", "")) if values["provider"] != "ollama" else ""
    return Client(values["provider"], values["url"], key)


class Client:
    def __init__(self, provider, url, key=""):
        validate({"provider": provider, "url": url})
        self.provider, self.url, self.key = provider, url, key

    def _request(self, path, body=None, timeout=600):
        if self.provider == "openai" and not self.key:
            raise AIError("Select your API key in Fabric Settings > AI Services > API credentials.")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(endpoint(self.url) + path, data=data, headers=headers)
        try:
            # Never redirect an API key or deed to a different endpoint, or use an ambient proxy.
            with ollama._open(req, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Service error bodies can echo prompts or keys; keep them out of the UI/log.
            error.close()
            raise AIError("AI service returned HTTP %d. Check the endpoint, credentials and model support."
                          % error.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise AIError("Could not read the AI service response. Check the server and connection.") from None

    def list_models(self):
        if self.provider == "ollama":
            return ollama.list_models(self.url)
        data = self._request("/models", timeout=10)
        try:
            return sorted(item["id"] for item in data["data"] if isinstance(item.get("id"), str))
        except (KeyError, TypeError, AttributeError):
            raise AIError("The service did not return an OpenAI-compatible model list.") from None

    def chat(self, model, prompt, images_b64=None, schema=None):
        if not model.strip():
            raise AIError("Choose a model in AI Services or Read Deed.")
        if self.provider == "ollama":
            return ollama.chat(self.url, model, prompt, images_b64, schema)
        content = prompt
        if images_b64:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}}
                for image in images_b64]
        messages = [{"role": "user", "content": content}]
        if schema is not None:
            messages.insert(0, {"role": "system", "content": "Return only JSON matching this schema: "
                             + json.dumps(schema)})
        body = {"model": model, "messages": messages, "stream": False}
        result = self._request("/chat/completions", body)
        try:
            text = result["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
            return text
        except (KeyError, IndexError, TypeError, ValueError):
            raise AIError("The model returned no text. Use a chat model with image support for page reading.") from None

    def chat_json(self, model, prompt, schema):
        if self.provider == "ollama":
            result = ollama.chat_json(self.url, model, prompt, schema)
        else:
            text = self.chat(model, prompt, schema=schema).strip()
            if text.startswith("```") and text.endswith("```"):
                text = text[3:-3].strip()
                if text.startswith("json"):
                    text = text[4:].lstrip()
            try:
                result = json.loads(text)
            except ValueError:
                raise AIError("The model did not return JSON. Try another call-extraction model.") from None
        if not isinstance(result, dict):
            raise AIError("The model did not return a JSON object. Try another call-extraction model.")
        calls = result.get("calls")
        if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
            raise AIError("The model did not return a list of calls. Try another call-extraction model.")
        for call in calls:
            if (any(not isinstance(call.get(key), str) for key in ("quote", "kind", "bearing"))
                    or call["kind"] not in ("line", "curve")
                    or any(call.get(key) is not None and not isinstance(call[key], str)
                           for key in ("delta", "side", "bearing_is"))):
                raise AIError("The model returned invalid call fields. Try another call-extraction model.")
        return result
