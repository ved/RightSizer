"""Minimal Prometheus HTTP client (standard library only)."""
import json
import urllib.error
import urllib.parse
import urllib.request


class PromError(RuntimeError):
    """Raised when Prometheus cannot be reached or rejects a query."""


class Prom:
    def __init__(self, url="http://127.0.0.1:9090", timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def query(self, promql):
        """Run an instant query and return the list of result series."""
        qs = urllib.parse.urlencode({"query": promql})
        full = f"{self.url}/api/v1/query?{qs}"
        try:
            with urllib.request.urlopen(full, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.load(e).get("error", "")
            except (ValueError, OSError):
                pass
            raise PromError(f"Prometheus rejected query ({e.code}): {detail}\n  {promql}") from e
        except OSError as e:
            raise PromError(f"cannot reach Prometheus at {self.url}: {e}") from e
        if body.get("status") != "success":
            raise PromError(f"query failed: {body.get('error')}\n  {promql}")
        return body["data"]["result"]

    def query_range(self, promql, start, end, step):
        """Run a range query and return the list of result series (each has "values")."""
        qs = urllib.parse.urlencode({"query": promql, "start": start, "end": end, "step": step})
        full = f"{self.url}/api/v1/query_range?{qs}"
        try:
            with urllib.request.urlopen(full, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise PromError(f"Prometheus rejected range query ({e.code})\n  {promql}") from e
        except OSError as e:
            raise PromError(f"cannot reach Prometheus at {self.url}: {e}") from e
        if body.get("status") != "success":
            raise PromError(f"range query failed: {body.get('error')}\n  {promql}")
        return body["data"]["result"]
