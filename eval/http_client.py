"""评测 HTTP 客户端：显式目标/凭据、无重试、禁止重定向、结构化失败。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import urllib.request


class ConfigurationError(ValueError):
    pass


def validate_base(base: str | None) -> str:
    if not base or not isinstance(base, str) or base != base.strip() or any(c.isspace() for c in base):
        raise ConfigurationError("请显式指定 --base 或 KNOWLEDGE_BASE，不使用默认远程服务")
    try:
        url = urlsplit(base)
        port = url.port
    except ValueError:
        raise ConfigurationError("测试服务地址格式无效") from None
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ConfigurationError("测试地址必须为无凭据、查询参数或片段的 HTTP(S) base URL")
    return base.rstrip("/")


def validate_api_key(key: str | None) -> str:
    if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ConfigurationError("请通过环境变量配置非空ASCII密钥；不接受空格或控制字符，不提供兜底")
    return key


class Redactor:
    """不保存认证头；脱敏凭据字段及已知凭据字面值。不是通用PII识别器。"""
    SENSITIVE = {"apikey", "xapikey", "authorization", "password", "secret", "token",
                 "accesstoken", "refreshtoken", "credential", "credentials", "databaseurl", "clientsecret", "secretkey",
                 "querykey", "managementkey", "knowledgequerykey", "knowledgemanagementkey",
                 "knowledgedatabaseurl", "knowledgeevaldatabaseurl"}

    def __init__(self, *secrets: str):
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def text(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        # 非JSON错误正文也可能回显认证字段。
        return re.sub(r'''(?i)(\b(?:x-api-key|api_key|apikey|authorization|password|access_token)\b["']?\s*[:=]\s*)[^\r\n,;<}]+''',
                      r'\1[REDACTED]', text)

    def value(self, value):
        if isinstance(value, dict):
            return {self.text(str(k)): ("[REDACTED]" if re.sub(r"[^a-z]", "", str(k).lower()) in self.SENSITIVE
                                      else self.value(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


@dataclass
class HttpOutcome:
    status: str
    latency_seconds: float
    http_status: int | None = None
    body: object = None
    text: str | None = None  # 仅非JSON正文；写入文件之前统一脱敏。
    error: dict | None = None


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("响应JSON字段重复")
        result[key] = value
    return result


class SearchFailure(RuntimeError):
    def __init__(self, outcome: HttpOutcome):
        self.outcome = outcome
        super().__init__(outcome.error["message"] if outcome.error else "检索请求失败")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不将 X-API-Key 转发到另一地址，也不偷偷改变被测目标。
        return None


def _protocol_error(body) -> str | None:
    if not isinstance(body, dict) or not isinstance(body.get("results"), list) or type(body.get("noEvidence")) is not bool:
        return "响应缺少 results 列表或 noEvidence 布尔值"
    if not isinstance(body.get("meta", {}), dict):
        return "响应 meta 必须为对象"
    if bool(body["results"]) == body["noEvidence"]:
        return "results空/非空与noEvidence声明矛盾"
    for item in body["results"]:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ("chunkId", "documentId")):
            return "结果缺少有效的 chunkId/documentId"
        if type(item.get("documentVersion")) is not int or item["documentVersion"] < 1:
            return "结果缺少有效的 documentVersion"
        score = item.get("score")
        if type(score) not in (int, float) or not math.isfinite(score):
            return "结果 score 不是有限数值"
    return None


@dataclass
class SearchClient:
    base_url: str
    api_key: str = field(repr=False)
    timeout: float = 120.0

    def __post_init__(self):
        self.base_url = validate_base(self.base_url)
        validate_api_key(self.api_key)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigurationError("timeout 必须为正的有限秒数")

    @classmethod
    def from_environment(cls, base=None, *, key_env="KNOWLEDGE_QUERY_KEY", timeout=120.0):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ConfigurationError("查询密钥环境变量名无效")
        return cls(base if base is not None else os.environ.get("KNOWLEDGE_BASE"), os.environ.get(key_env, ""), timeout)

    @property
    def endpoint(self):
        return self.base_url + "/knowledge/search"

    def request(self, body: dict) -> HttpOutcome:
        request = urllib.request.Request(self.endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers={"X-API-Key": self.api_key, "Content-Type": "application/json; charset=utf-8"})
        started = time.perf_counter()
        http_status, raw = None, None
        try:
            try:
                with urllib.request.build_opener(NoRedirectHandler()).open(request, timeout=self.timeout) as response:
                    http_status = response.status
                    raw = response.read()
            except HTTPError as exc:
                http_status = exc.code
                try:
                    raw = exc.read()
                finally:
                    exc.close()
            text = raw.decode("utf-8")
            try:
                # 拒绝 Python JSON 解析器默认允许的 NaN/Infinity。
                body = json.loads(text, object_pairs_hook=_json_object, parse_constant=lambda s: (_ for _ in ()).throw(ValueError("非有限JSON数值")))
            except ValueError:
                return HttpOutcome("HTTP_ERROR" if not 200 <= http_status < 300 else "INVALID_RESPONSE",
                                   time.perf_counter() - started, http_status, text=text,
                                   error={"code": "INVALID_JSON", "message": "响应不是有效JSON"})
            if not 200 <= http_status < 300:
                return HttpOutcome("HTTP_ERROR", time.perf_counter() - started, http_status, body,
                                   error={"code": "HTTP_STATUS", "message": f"服务返回 HTTP {http_status}"})
            error = _protocol_error(body)
            return HttpOutcome("INVALID_RESPONSE" if error else "SUCCEEDED", time.perf_counter() - started,
                               http_status, body, error={"code": "INVALID_SCHEMA", "message": error} if error else None)
        except UnicodeDecodeError:
            return HttpOutcome("HTTP_ERROR" if http_status is not None and not 200 <= http_status < 300 else "INVALID_RESPONSE",
                               time.perf_counter() - started, http_status, text=raw.decode("utf-8", errors="replace"),
                               error={"code": "INVALID_UTF8", "message": "响应不是UTF-8文本"})
        except (TimeoutError, URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            timeout = isinstance(reason, TimeoutError)
            return HttpOutcome("TIMEOUT" if timeout else "NETWORK_ERROR", time.perf_counter() - started,
                               http_status, error={"code": type(reason).__name__,
                                                   "message": "请求超时" if timeout else "网络请求失败"})

    def search(self, body: dict):
        outcome = self.request(body)
        if outcome.status != "SUCCEEDED":
            raise SearchFailure(outcome)
        return outcome.body["results"], outcome.latency_seconds, outcome.body
