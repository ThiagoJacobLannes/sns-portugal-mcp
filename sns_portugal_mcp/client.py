"""HTTP client for the SNS open-data sources.

Two public, keyless sources:

* SNS Transparency portal (Opendatasoft Explore API v2.1):
  https://transparencia.sns.gov.pt/api/explore/v2.1 — ~150 datasets, mostly monthly
  hospital and primary-care statistics published by the SNS Executive Direction.
* Institution list behind tempos.min-saude.pt: https://tempos.min-saude.pt/institutions.json
  — hospitals and ULS (local health units) with coordinates and which data they report.

We deliberately do NOT call tempos.min-saude.pt/api/* (it needs an API key embedded in
their web app) and do NOT scrape the live emergency waiting-time report.

Catalog, institution list and institution-name lists are cached in memory with a TTL.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from typing import Any, Callable

import httpx

log = logging.getLogger("sns_portugal_mcp")

ODS_BASE = "https://transparencia.sns.gov.pt/api/explore/v2.1"
PORTAL_URL = "https://transparencia.sns.gov.pt"
INSTITUTIONS_URL = "https://tempos.min-saude.pt/institutions.json"

CACHE_TTL = 6 * 3600  # seconds; the data changes monthly
MAX_LIMIT = 100       # Opendatasoft's per-request cap for plain record queries

HEADERS = {
    "User-Agent": "sns-portugal-mcp/0.1 (open-source MCP server; read-only)",
    "Accept": "application/json",
}


class SNSError(Exception):
    """A clean, model-facing error (bad input or upstream failure)."""


# --------------------------------------------------------------------------- helpers

def normalize(text: str | None) -> str:
    """Lowercase, strip accents and punctuation: 'Saúde, EPE' -> 'saude epe'."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"\buls\b", "unidade local de saude", t)
    t = re.sub(r"\bipo\b", "instituto portugues oncologia", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# Words that say nothing about WHICH hospital it is.
_STOP = {
    "de", "da", "do", "das", "dos", "e", "a", "o", "epe", "e p e", "ep", "ppp", "sa", "ip",
    "unidade", "local", "saude", "hospital", "hospitais", "hospitalar", "centro",
    "universitario", "servico", "urgencia", "basica", "sub",
}


def tokens(text: str | None) -> set[str]:
    return {w for w in normalize(text).split() if w not in _STOP and len(w) > 1}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def ods_quote(value: str) -> str:
    """Quote a string literal for an ODSQL where clause."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def month_str(value: Any) -> str | None:
    """'2026-07-01T00:00:00+00:00' or '2026-07' -> '2026-07'."""
    if not value:
        return None
    m = re.match(r"(\d{4})-(\d{2})", str(value))
    return f"{m.group(1)}-{m.group(2)}" if m else str(value)


def parse_period(period: str | None) -> str | None:
    """Validate 'YYYY-MM' (or 'YYYY') and return an ODSQL date literal for its first day."""
    if not period:
        return None
    p = period.strip()
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?", p)
    if not m:
        raise SNSError(f"period must look like 'YYYY-MM' (e.g. '2026-07'), got {period!r}")
    year, month = int(m.group(1)), int(m.group(2) or 12)
    if not 1 <= month <= 12:
        raise SNSError(f"invalid month in period {period!r}")
    return f"date'{year:04d}-{month:02d}-01'"


def strip_html(text: str | None, max_len: int | None = None) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = (
        t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    t = re.sub(r"\s+", " ", t).strip()
    if max_len and len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t


class _TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: str, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (now, value)
        return value


# --------------------------------------------------------------------------- client

class SNSClient:
    def __init__(self, base_url: str = ODS_BASE, institutions_url: str = INSTITUTIONS_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.institutions_url = institutions_url
        self._http = httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True)
        self._cache = _TTLCache(CACHE_TTL)

    # ---- raw HTTP

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        # Opendatasoft treats an empty parameter (e.g. group_by=) as a real, empty clause.
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        try:
            r = self._http.get(url, params=clean)
        except httpx.HTTPError as exc:
            raise SNSError(f"could not reach {url.split('/api')[0]}: {exc}") from exc
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            msg = data.get("message") if isinstance(data, dict) else None
            raise SNSError(f"upstream error {r.status_code}: {msg or r.text[:300]}")
        if data is None:
            raise SNSError(f"upstream returned non-JSON from {url}")
        return data

    # ---- transparency portal

    def records(self, dataset_id: str, **params: Any) -> dict[str, Any]:
        """GET /catalog/datasets/{id}/records with ODSQL params (where, select, group_by, ...)."""
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", dataset_id or ""):
            raise SNSError(f"invalid dataset_id {dataset_id!r}")
        return self._get_json(f"{self.base_url}/catalog/datasets/{dataset_id}/records", params)

    def catalog(self) -> list[dict[str, Any]]:
        """All datasets (id, title, description, fields, licence...). Cached."""

        def load() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = self._get_json(
                    f"{self.base_url}/catalog/datasets",
                    {"limit": MAX_LIMIT, "offset": offset},
                )
                results = page.get("results") or []
                for d in results:
                    m = (d.get("metas") or {}).get("default") or {}
                    out.append({
                        "dataset_id": d.get("dataset_id"),
                        "title": m.get("title"),
                        "description": strip_html(m.get("description")),
                        "themes": m.get("theme") or [],
                        "keywords": m.get("keyword") or [],
                        "publisher": m.get("publisher"),
                        "license": m.get("license"),
                        "license_url": m.get("license_url"),
                        "modified": m.get("modified"),
                        "records_count": m.get("records_count"),
                        "fields": [
                            {"name": f.get("name"), "label": f.get("label"), "type": f.get("type")}
                            for f in d.get("fields") or []
                        ],
                    })
                offset += len(results)
                if not results or offset >= (page.get("total_count") or 0):
                    break
            log.info("loaded dataset catalog: %d datasets", len(out))
            return out

        return self._cache.get_or_set("catalog", load)

    def dataset_meta(self, dataset_id: str) -> dict[str, Any] | None:
        return next((d for d in self.catalog() if d["dataset_id"] == dataset_id), None)

    def entity_names(self, dataset_id: str, field: str = "instituicao", time_field: str = "tempo") -> list[dict[str, Any]]:
        """Distinct institution names in a dataset with their latest period. Cached."""

        def load() -> list[dict[str, Any]]:
            data = self.records(
                dataset_id,
                select=f"{field} as name, max({time_field}) as latest",
                group_by=field,
                limit=1000,
            )
            return [
                {"name": r["name"], "latest": month_str(r.get("latest"))}
                for r in data.get("results") or []
                if r.get("name")
            ]

        return self._cache.get_or_set(f"names:{dataset_id}:{field}", load)

    # ---- tempos.min-saude.pt static institution list

    def institutions(self) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            data = self._get_json(self.institutions_url)
            if not isinstance(data, list):
                raise SNSError("unexpected institutions.json format")
            log.info("loaded institution list: %d entries", len(data))
            return data

        return self._cache.get_or_set("institutions", load)

    # ---- name resolution

    def resolve_entity(self, query: str, dataset_ids: list[str]) -> dict[str, Any]:
        """Map a free-text hospital/ULS name to the exact `instituicao` used in the statistics.

        Tries the statistics' own names first; if that fails (e.g. "Hospital de Faro", which
        reports under "ULS do Algarve"), looks the hospital up in the institution list and
        retries with its ULS name.
        """
        names: dict[str, str | None] = {}
        for ds in dataset_ids:
            for n in self.entity_names(ds):
                prev = names.get(n["name"])
                names[n["name"]] = max(filter(None, [prev, n["latest"]]), default=None)

        def best(q: str) -> list[str]:
            qn, qt = normalize(q), tokens(q)
            scored = []
            for name, latest in names.items():
                if qn and qn in normalize(name):
                    score = 2.0
                elif qt:
                    score = len(qt & tokens(name)) / len(qt)
                else:
                    score = 0.0
                if score >= 0.99:
                    # Ties: prefer institutions still reporting (post-2024 ULS over old names).
                    scored.append((score, latest or "", name))
            return [name for *_, name in sorted(scored, reverse=True)]

        ranked = best(query)
        via = None
        if not ranked:
            inst = self.match_institutions(query, limit=1)
            if inst:
                parent = inst[0].get("ulsName") or inst[0].get("name")
                ranked = best(parent)
                via = inst[0].get("name")
        if not ranked:
            raise SNSError(
                f"no institution in the statistics matches {query!r}. "
                "Try a ULS name (e.g. 'Santa Maria', 'Algarve', 'Coimbra') or use find_institutions."
            )
        return {
            "name": ranked[0],
            "latest_period": names.get(ranked[0]),
            "matched_via_hospital": via,
            "other_candidates": ranked[1:4],
        }

    def match_institutions(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        qn, qt = normalize(query), tokens(query)
        scored = []
        for inst in self.institutions():
            hay = f"{inst.get('name') or ''} {inst.get('ulsName') or ''}"
            hn = normalize(hay)
            if qn and qn in hn:
                score = 2.0 if qn in normalize(inst.get("name")) else 1.5
            elif qt:
                score = len(qt & tokens(hay)) / len(qt)
            else:
                score = 0.0
            if score >= 0.5:
                scored.append((score, inst))
        scored.sort(key=lambda s: (-s[0], not s[1].get("hasEmergencyData"), s[1].get("name") or ""))
        return [i for _, i in scored[:limit]]
