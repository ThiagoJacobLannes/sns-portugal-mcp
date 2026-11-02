"""FastMCP stdio server: official SNS (Portugal) open-data statistics — read-only."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import (
    INSTITUTIONS_URL,
    ODS_BASE,
    PORTAL_URL,
    SNSClient,
    SNSError,
    haversine_km,
    month_str,
    normalize,
    ods_quote,
    parse_period,
)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s sns-portugal-mcp %(levelname)s %(message)s",
)
log = logging.getLogger("sns_portugal_mcp")

LIVE_WAITS_URL = "https://tempos.min-saude.pt"

INSTRUCTIONS = (
    "Official open-data statistics about Portugal's national health service (SNS): hospitals and "
    "local health units (ULS), emergency-department visits and Manchester triage colours, surgery "
    "and first-consultation access indicators, plus generic access to the ~150 datasets of the SNS "
    "Transparency portal. Figures are monthly official statistics (the latest month is usually 2-3 "
    "months back and provisional) — NOT live emergency-room queues; for live waits point the user to "
    f"{LIVE_WAITS_URL}. This is not medical advice: in an emergency call 112; for health advice call "
    "SNS 24 on 808 24 24 24."
)

STATS_NOTE = (
    "Official monthly statistics, not live queues (live ER waits: "
    f"{LIVE_WAITS_URL}). The source publishes year-to-date cumulative values; 'in_month' figures are "
    "derived as the difference between consecutive months. The last ~2 months are provisional."
)

mcp = FastMCP("sns-portugal", instructions=INSTRUCTIONS)

_client: SNSClient | None = None


def client() -> SNSClient:
    global _client
    if _client is None:
        _client = SNSClient(
            os.environ.get("SNS_ODS_BASE_URL", ODS_BASE),
            os.environ.get("SNS_INSTITUTIONS_URL", INSTITUTIONS_URL),
        )
    return _client


# --------------------------------------------------------------------------- dataset specs
# kind "ytd": the source value is cumulative since January; "snapshot": value at end of month.

ER_TYPES = {
    "id": "atendimentos-por-tipo-de-urgencia-hospitalar-link",
    "kind": "ytd",
    "fields": {
        "general": "urgencias_geral",
        "paediatric": "urgencias_pediatricas",
        "obstetric": "urgencia_obstetricia",
        "psychiatric": "urgencia_psiquiatrica",
        "total": "total_urgencias",
    },
}
ER_TRIAGE = {
    "id": "atendimentos-em-urgencia-triagem-manchester",
    "kind": "ytd",
    "fields": {
        "red": "no_de_atendimentos_em_urgencia_su_triagem_manchester_vermelha",
        "orange": "no_de_atendimentos_em_urgencia_su_triagem_manchester_laranja",
        "yellow": "no_de_atendimentos_em_urgencia_su_triagem_manchester_amarela",
        "green": "no_de_atendimentos_em_urgencia_su_triagem_manchester_verde",
        "blue": "no_de_atendimentos_em_urgencia_su_triagem_manchester_azul",
        "white": "no_de_atendimentos_em_urgencia_su_triagem_manchester_branca",
        "not_triaged": "no_de_atendimentos_em_sem_triagem_manchester",
    },
}
PREOP = {
    "id": "demora-media-antes-da-cirurgia",
    "kind": "ytd",
    "fields": {
        "days": "no_de_dias_ate_cirurgia_em_episodios_de_gdh_cirurgicos_programados",
        "episodes": "no_de_episodios_em_gdh_cirurgicos_de_internamento_programados_com_exclusoes",
    },
}
SURGERIES = {
    "id": "intervencoes-cirurgicas",
    "kind": "ytd",
    "fields": {
        "scheduled": "no_intervencoes_cirurgicas_programadas",
        "scheduled_inpatient": "no_intervencoes_cirurgicas_convencionais",
        "scheduled_day_surgery": "no_intervencoes_cirurgicas_de_ambulatorio",
        "urgent": "no_intervencoes_cirurgicas_urgentes",
    },
}
SURGERY_LIST = {
    "id": "inscritos-em-lic-dentro-do-tmrg-180-dias",
    "kind": "snapshot",
    "fields": {
        "within_max_time": "no_de_doentes_inscritos_dentro_do_tmrg_sigic",
        "waiting": "no_de_doentes_inscritos_sigic",
    },
}
FIRST_CONSULT_TIMELY = {
    "id": "consultas-em-tempo-real",
    "kind": "ytd",
    "fields": {
        "within_max_time": "no_primeiras_ce_prestadas_dentro_do_tmrg",
        "total": "no_primeiras_ce_realizadas_com_registo_no_cth",
    },
}
CONSULTS = {
    "id": "01_sica_evolucao-mensal-das-consultas-medicas-hospitalares",
    "kind": "ytd",
    "fields": {
        "total": "no_de_consultas_medicas_total",
        "first": "no_de_primeiras_consultas",
        "subsequent": "no_de_consultas_subsequentes",
    },
}

REGIONS = {
    "norte": "Norte", "north": "Norte", "porto": "Norte",
    "centro": "Centro", "center": "Centro", "centre": "Centro", "coimbra": "Centro",
    "lvt": "LVT", "lisboa": "LVT", "lisbon": "LVT", "lisboa e vale do tejo": "LVT",
    "alentejo": "Alentejo", "algarve": "Algarve",
}


def _region(region: str | None) -> str | None:
    if not region:
        return None
    key = normalize(region).replace("regiao de saude ", "").replace("ars ", "").strip()
    if key not in REGIONS:
        raise SNSError(f"unknown region {region!r}; use one of Norte, Centro, LVT (Lisboa), Alentejo, Algarve")
    return REGIONS[key]


def _num(v: Any) -> Any:
    """Aggregates come back as floats (e.g. 0.0); show whole numbers as ints."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _prev_month(p: str) -> str:
    y, m = int(p[:4]), int(p[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _series(spec: dict[str, Any], entity: str | None, region: str | None,
            period: str | None, months: int) -> dict[str, dict[str, Any]]:
    """Fetch {period: {"ytd"/"value": {...}, "in_month": {...}, "n": k}} latest-first."""
    where = []
    if entity:
        where.append(f"instituicao = {ods_quote(entity)}")
    if region:
        where.append(f"regiao like {ods_quote(region)}")
    end = parse_period(period)
    if end:
        where.append(f"tempo <= {end}")
    sel = ", ".join(f"sum({src}) as {out}" for out, src in spec["fields"].items())
    data = client().records(
        spec["id"],
        select=f"tempo, {sel}, count(*) as n",
        where=" and ".join(where) or None,
        group_by="tempo",
        order_by="tempo desc",
        limit=months + 1,
    )
    rows = {month_str(r["tempo"]): r for r in data.get("results") or []}
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(rows, reverse=True)[:months]:
        cur = {k: _num(rows[p].get(k)) for k in spec["fields"]}
        entry: dict[str, Any] = {"n": rows[p].get("n")}
        if spec["kind"] == "snapshot":
            entry["value"] = cur
        else:
            entry["ytd"] = cur
            prev = rows.get(_prev_month(p))
            if p.endswith("-01"):
                entry["in_month"] = dict(cur)
            elif prev:
                diff = {}
                for k, v in cur.items():
                    pv = prev.get(k)
                    d = v - pv if isinstance(v, (int, float)) and isinstance(pv, (int, float)) else None
                    diff[k] = d if d is None or d >= 0 else None
                entry["in_month"] = diff
            else:
                entry["in_month"] = None
        out[p] = entry
    return out


def _pct(num: Any, den: Any, nd: int = 1) -> float | None:
    if not isinstance(num, (int, float)) or not isinstance(den, (int, float)) or den <= 0:
        return None
    return round(100.0 * num / den, nd)


def _ratio(num: Any, den: Any, nd: int = 2) -> float | None:
    if not isinstance(num, (int, float)) or not isinstance(den, (int, float)) or den <= 0:
        return None
    return round(num / den, nd)


def _scope(institution: str | None, region: str | None, specs: list[dict[str, Any]]) -> tuple[str | None, str | None, dict[str, Any]]:
    entity = None
    info: dict[str, Any]
    reg = _region(region)
    if institution:
        res = client().resolve_entity(institution, [s["id"] for s in specs])
        entity = res["name"]
        info = {"level": "institution", "institution": entity}
        if res["matched_via_hospital"]:
            info["matched_via_hospital"] = res["matched_via_hospital"]
            info["note"] = "Statistics are published per ULS / hospital group, not per individual hospital building."
        if res["other_candidates"]:
            info["other_candidates"] = res["other_candidates"]
    elif reg:
        info = {"level": "region", "region": reg}
    else:
        info = {"level": "national"}
    return entity, reg, info


def _sources(specs: list[dict[str, Any]], series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "dataset_id": s["id"],
            "latest_period_returned": max(data, default=None),
            "url": f"{PORTAL_URL}/explore/dataset/{s['id']}/",
        }
        for s, data in zip(specs, series)
    ]


_SEARCH_STOP = {"de", "da", "do", "das", "dos", "e", "a", "o", "em", "no", "na", "por", "para", "com", "the", "of"}


def _words(text: str | None) -> set[str]:
    return {w for w in normalize(text).replace("_", " ").split() if w not in _SEARCH_STOP and len(w) > 1}


def _months(months: int) -> int:
    return max(1, min(int(months or 6), 24))


# --------------------------------------------------------------------------- tools

@mcp.tool()
def find_institutions(
    query: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    emergency_only: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Find SNS hospitals / local health units (ULS) by name, or the nearest ones to a coordinate.

    Use this first when the user names a hospital or a place ("hospitals near Lisbon",
    "is there an emergency department near 38.72,-9.14?"), then pass `stats_entity` to
    emergency_stats / surgery_wait_stats / consultation_wait_stats.

    Each result: name, its parent ULS, coordinates, distance_km (when lat/lon given), whether
    it reports emergency / consultation / surgery data on tempos.min-saude.pt, and
    `stats_entity` — the name under which the official statistics report it (statistics are
    per ULS / hospital group, not per building). This list does NOT contain live waiting times;
    for live emergency waits send the user to https://tempos.min-saude.pt.

    Args:
        query:          Part of the name, accent-insensitive, e.g. "Santa Maria", "Faro", "ULS Algarve".
        lat, lon:       Sort by distance from this point (WGS84), e.g. Lisbon 38.7223, -9.1393.
                        Without `query`, umbrella ULS entries are skipped (they duplicate a hospital's location).
        emergency_only: Only places that report emergency-department data.
        limit:          Max results (default 10, max 50).
    """
    try:
        c = client()
        limit = max(1, min(int(limit or 10), 50))
        if (lat is None) != (lon is None):
            return {"error": "give both lat and lon, or neither"}
        if query:
            items = c.match_institutions(query, limit=500)
        else:
            items = list(c.institutions())
            if lat is not None:
                items = [i for i in items if not i.get("isUls")]
        if emergency_only:
            items = [i for i in items if i.get("hasEmergencyData")]
        rows = []
        for i in items:
            row: dict[str, Any] = {
                "name": i.get("name"),
                "uls": i.get("ulsName") or (i.get("name") if i.get("isUls") else None),
                "is_uls": bool(i.get("isUls")),
                "lat": i.get("latitude"),
                "lon": i.get("longitude"),
                "has_emergency_data": bool(i.get("hasEmergencyData")),
                "has_consultation_data": bool(i.get("hasAppointmentData")),
                "has_surgery_data": bool(i.get("hasSurgeryData")),
                "website": i.get("website"),
            }
            if lat is not None and i.get("latitude") is not None:
                row["distance_km"] = round(haversine_km(lat, lon, float(i["latitude"]), float(i["longitude"])), 1)
            rows.append(row)
        if lat is not None:
            rows.sort(key=lambda r: r.get("distance_km", 1e9))
        elif not query:
            rows.sort(key=lambda r: r["name"] or "")
        rows = rows[:limit]
        for r in rows:
            try:
                r["stats_entity"] = c.resolve_entity(r["uls"] or r["name"], [ER_TYPES["id"], SURGERIES["id"]])["name"]
            except SNSError:
                r["stats_entity"] = None
        return {
            "count": len(rows),
            "institutions": rows,
            "source": INSTITUTIONS_URL,
            "live_waiting_times": LIVE_WAITS_URL,
        }
    except Exception as exc:  # noqa: BLE001 — surface a clean message to the model
        log.exception("find_institutions failed")
        return {"error": str(exc)}


@mcp.tool()
def emergency_stats(
    institution: str | None = None,
    region: str | None = None,
    period: str | None = None,
    months: int = 6,
) -> dict[str, Any]:
    """Emergency-department (urgência) visits per month: by type and by Manchester triage colour.

    Use for "how busy is the ER at X?", "how many emergency visits in the Algarve last month?",
    "what share of ER visits are green/blue (non-urgent)?". OFFICIAL MONTHLY STATISTICS ONLY —
    not the live queue (the latest month is usually 2-3 months back and provisional). For live
    waiting times send the user to https://tempos.min-saude.pt. Not medical advice: in an
    emergency call 112; SNS 24 (808 24 24 24) for advice.

    Scope: one institution, one region, or (neither) all of mainland Portugal's SNS hospitals.

    Per period (latest first): visits_in_month and visits_year_to_date by type (general,
    paediatric, obstetric, psychiatric, total), triage_in_month by colour (red = immediate,
    orange = very urgent, yellow = urgent, green = less urgent, blue = non-urgent, white = not
    clinical / other) and triage_share_in_month_pct.

    Args:
        institution: Hospital or ULS name, accent-insensitive ("Santa Maria", "São João",
                     "Hospital de Faro"). A hospital is mapped to its ULS; check `scope`.
        region:      Norte | Centro | LVT (Lisboa) | Alentejo | Algarve. Ignored if institution is given.
        period:      Latest month to include, "YYYY-MM" (default: newest available).
        months:      How many months back to return (default 6, max 24).
    """
    try:
        specs = [ER_TYPES, ER_TRIAGE]
        entity, reg, scope = _scope(institution, None if institution else region, specs)
        m = _months(months)
        types = _series(ER_TYPES, entity, reg, period, m)
        triage = _series(ER_TRIAGE, entity, reg, period, m)
        periods = []
        for p in sorted(set(types) | set(triage), reverse=True)[:m]:
            t, g = types.get(p) or {}, triage.get(p) or {}
            gm = g.get("in_month")
            tri_total = sum(v for v in (gm or {}).values() if isinstance(v, (int, float))) if gm else None
            row: dict[str, Any] = {
                "period": p,
                "visits_in_month": t.get("in_month"),
                "visits_year_to_date": t.get("ytd"),
                "triage_in_month": gm,
                "triage_share_in_month_pct": (
                    {k: _pct(v, tri_total) for k, v in gm.items()} if gm and tri_total else None
                ),
            }
            if scope["level"] != "institution":
                row["institutions_reporting"] = t.get("n") or g.get("n")
            periods.append(row)
        if not periods:
            return {"error": "no emergency statistics found for that scope/period", "scope": scope}
        return {
            "scope": scope,
            "periods": periods,
            "note": STATS_NOTE,
            "sources": _sources(specs, [types, triage]),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("emergency_stats failed")
        return {"error": str(exc)}


@mcp.tool()
def surgery_wait_stats(
    institution: str | None = None,
    region: str | None = None,
    period: str | None = None,
    months: int = 6,
) -> dict[str, Any]:
    """Surgery access indicators per month: surgical waiting list, % within the guaranteed
    maximum time, mean days from admission to surgery, and surgeries performed.

    Use for "how long is the wait for surgery at X?", "how many people are on the surgery list
    in the Norte region?", "how many operations did hospital Y do?". Official monthly statistics
    (latest month usually 2-3 months back, provisional). The open data has NO breakdown by
    specialty or procedure — say so if asked for one.

    Per period (latest first):
      - waiting_list: patients on the surgical waiting list (LIC) at month end, how many are
        still within the legal maximum guaranteed response time (TMRG, 180 days for normal
        priority since 2018) and the % within it.
      - mean_days_before_surgery_in_month / _year_to_date: average days between hospital
        admission and the operation for scheduled inpatient surgery (this is NOT the time on
        the waiting list).
      - surgeries_in_month / surgeries_year_to_date: scheduled (= inpatient + day surgery) and urgent.

    Args:
        institution: Hospital or ULS name ("Santa Maria", "Coimbra", "IPO Porto"); mapped to its ULS.
        region:      Norte | Centro | LVT (Lisboa) | Alentejo | Algarve. Ignored if institution is given.
        period:      Latest month to include, "YYYY-MM" (default: newest available).
        months:      How many months back (default 6, max 24).
    """
    try:
        specs = [SURGERY_LIST, PREOP, SURGERIES]
        entity, reg, scope = _scope(institution, None if institution else region, specs)
        m = _months(months)
        lic = _series(SURGERY_LIST, entity, reg, period, m)
        pre = _series(PREOP, entity, reg, period, m)
        surg = _series(SURGERIES, entity, reg, period, m)
        periods = []
        for p in sorted(set(lic) | set(pre) | set(surg), reverse=True)[:m]:
            lv = (lic.get(p) or {}).get("value")
            pr = pre.get(p) or {}
            prm, pry = pr.get("in_month"), pr.get("ytd")
            s = surg.get(p) or {}
            row: dict[str, Any] = {
                "period": p,
                "waiting_list": (
                    {
                        "patients_waiting": lv["waiting"],
                        "within_max_guaranteed_time": lv["within_max_time"],
                        "pct_within_max_guaranteed_time": _pct(lv["within_max_time"], lv["waiting"]),
                    } if lv else None
                ),
                "mean_days_before_surgery_in_month": _ratio(prm["days"], prm["episodes"]) if prm else None,
                "mean_days_before_surgery_year_to_date": _ratio(pry["days"], pry["episodes"]) if pry else None,
                "surgeries_in_month": s.get("in_month"),
                "surgeries_year_to_date": s.get("ytd"),
            }
            periods.append(row)
        if not periods:
            return {"error": "no surgery statistics found for that scope/period", "scope": scope}
        return {
            "scope": scope,
            "periods": periods,
            "note": STATS_NOTE + " The waiting list is a month-end snapshot.",
            "sources": _sources(specs, [lic, pre, surg]),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("surgery_wait_stats failed")
        return {"error": str(exc)}


@mcp.tool()
def consultation_wait_stats(
    institution: str | None = None,
    region: str | None = None,
    period: str | None = None,
    months: int = 6,
) -> dict[str, Any]:
    """Hospital outpatient consultations per month and the % of FIRST specialist consultations
    (referred by a family doctor via CTH/SIGA) held within the guaranteed maximum time (TMRG).

    Use for "how long is the wait for a first hospital appointment at X?", "is hospital Y on
    time with first consultations?", "how many consultations did the Centro region do?".
    Official monthly statistics (latest month usually 2-3 months back, provisional); there is no
    breakdown by specialty in the open data.

    Per period (latest first): consultations_in_month / _year_to_date (total, first,
    subsequent) and first_consultations_within_max_time_pct for the month and year-to-date.

    Args:
        institution: Hospital or ULS name; mapped to its ULS.
        region:      Norte | Centro | LVT (Lisboa) | Alentejo | Algarve. Ignored if institution is given.
        period:      Latest month to include, "YYYY-MM" (default: newest available).
        months:      How many months back (default 6, max 24).
    """
    try:
        specs = [FIRST_CONSULT_TIMELY, CONSULTS]
        entity, reg, scope = _scope(institution, None if institution else region, specs)
        m = _months(months)
        timely = _series(FIRST_CONSULT_TIMELY, entity, reg, period, m)
        cons = _series(CONSULTS, entity, reg, period, m)
        periods = []
        for p in sorted(set(timely) | set(cons), reverse=True)[:m]:
            t, c = timely.get(p) or {}, cons.get(p) or {}
            tm, ty = t.get("in_month"), t.get("ytd")
            periods.append({
                "period": p,
                "consultations_in_month": c.get("in_month"),
                "consultations_year_to_date": c.get("ytd"),
                "first_consultations_within_max_time_pct_in_month": _pct(tm["within_max_time"], tm["total"]) if tm else None,
                "first_consultations_within_max_time_pct_year_to_date": _pct(ty["within_max_time"], ty["total"]) if ty else None,
                "first_consultations_referred_in_month": tm["total"] if tm else None,
            })
        if not periods:
            return {"error": "no consultation statistics found for that scope/period", "scope": scope}
        return {
            "scope": scope,
            "periods": periods,
            "note": STATS_NOTE,
            "sources": _sources(specs, [timely, cons]),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("consultation_wait_stats failed")
        return {"error": str(exc)}


@mcp.tool()
def search_datasets(query: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Search the ~150 datasets of the SNS Transparency portal (titles and descriptions are in Portuguese).

    Use when the curated tools don't cover the question — e.g. hospital bed occupancy
    ("ocupação"), births/caesareans ("partos"), INEM pre-hospital calls, SNS 24 calls, family
    doctor coverage ("médico de família", "inscritos"), medicines spending, staff ("trabalhadores"),
    cancer screening ("rastreios"). Then call query_dataset with the dataset_id and the field names
    returned here. Portuguese keywords work best; matching is accent-insensitive.

    Args:
        query: Keywords, e.g. "ocupação internamento", "partos", "urgência". Empty = list all (id + title).
        limit: Max results (default 10, max 50; ignored when listing all).
    """
    try:
        cat = client().catalog()
        if not query or not query.strip():
            return {
                "count": len(cat),
                "datasets": [{"dataset_id": d["dataset_id"], "title": d["title"]} for d in cat],
            }
        limit = max(1, min(int(limit or 10), 50))
        q = _words(query)
        scored = []
        for d in cat:
            title = _words(f"{d['title']} {d['dataset_id']} {' '.join(d['keywords'])} {' '.join(d['themes'])}")
            desc = _words(d["description"])
            fields = _words(" ".join(f"{f['name']} {f['label']}" for f in d["fields"]))
            score = sum(
                3 if any(w.startswith(t) for w in title)
                else 1 if any(w.startswith(t) for w in desc | fields) else 0
                for t in q
            )
            if score:
                scored.append((score, d.get("modified") or "", d))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        return {
            "count": len(scored[:limit]),
            "datasets": [
                {
                    "dataset_id": d["dataset_id"],
                    "title": d["title"],
                    "description": d["description"][:400],
                    "records": d["records_count"],
                    "last_modified": (d["modified"] or "")[:10],
                    "license": d["license"],
                    "fields": [f"{f['name']} ({f['type']}): {f['label']}" for f in d["fields"]],
                }
                for _, _, d in scored[:limit]
            ],
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("search_datasets failed")
        return {"error": str(exc)}


@mcp.tool()
def query_dataset(
    dataset_id: str,
    where: str | None = None,
    select: str | None = None,
    group_by: str | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Run an ODSQL query on any SNS Transparency portal dataset (Opendatasoft Explore API v2.1).

    Use after search_datasets for anything the curated tools don't cover. Clauses are ODSQL:
      - where:    `instituicao like "santa maria" and tempo >= date'2026-01-01'`
                  (`like` is a case- and accent-insensitive "contains"; dates use date'YYYY-MM-DD')
      - select:   `tempo, instituicao, total_urgencias` or aggregates `sum(total_urgencias) as total`
      - group_by: `regiao` or `tempo`  (combine with aggregates in select)
      - order_by: `tempo desc`
    Most hospital-activity datasets (field `tempo`, monthly) store YEAR-TO-DATE CUMULATIVE
    values: December is the annual total; a single month = this month minus the previous one.
    Many datasets have a `regiao` and an `instituicao` (or `entidade`) field. Official statistics,
    not live data.

    Args:
        dataset_id: e.g. "ocupacao-do-internamento" (from search_datasets).
        where, select, group_by, order_by: ODSQL clauses (optional).
        limit:  Rows to return (default 20, max 100).
        offset: Skip rows, for paging.
    """
    try:
        c = client()
        limit = max(1, min(int(limit or 20), 100))
        data = c.records(
            dataset_id,
            where=where,
            select=select,
            group_by=group_by,
            order_by=order_by,
            limit=limit,
            offset=max(0, int(offset or 0)) or None,
        )
        meta = c.dataset_meta(dataset_id) or {}
        results = data.get("results") or []
        return {
            "dataset_id": dataset_id,
            "title": meta.get("title"),
            "total_count": data.get("total_count"),
            "count": len(results),
            "results": results,
            "license": meta.get("license"),
            "url": f"{PORTAL_URL}/explore/dataset/{dataset_id}/",
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("query_dataset failed")
        return {"error": str(exc)}


def main() -> None:
    log.warning("sns-portugal-mcp is deprecated and gets no more updates: use portugal-mcp instead (uvx portugal-mcp; PORTUGAL_MCP_FEATURES=saude loads only this part).")
    mcp.run()


if __name__ == "__main__":
    main()
