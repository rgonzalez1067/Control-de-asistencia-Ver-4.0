"""Endpoints de estadísticas y reportes (executive summary, dashboard, CSV export).

Migrado desde server.py durante la Fase A · Iteración 2 (feb-2026).
"""
import io
import csv
from deps import (
    api, db, get_current_user, require_roles,
    LEADER_ROLES, APP_TZ,
    now_utc, strip_mongo_id, supervisor_scope_ids, _parse_date_range,
    HTTPException, Depends, Query,
    Any, Dict, List, Optional,
    datetime, timezone, timedelta,
)
from fastapi.responses import StreamingResponse


def _process_late_record(r: Dict[str, Any], u_info: Dict[str, Any], depts: Dict[str, str], per_user: Dict[str, Dict[str, Any]]) -> int:
    minutes = int(r.get("late_minutes") or 0)
    uid = r.get("user_id", "")
    u = per_user.setdefault(uid, {
        "user_id": uid,
        "name": u_info.get("name", uid),
        "cedula": u_info.get("cedula"),
        "position": u_info.get("position"),
        "department_name": depts.get(u_info.get("department_id") or "", "Sin departamento"),
        "late_count": 0,
        "total_minutes": 0,
    })
    u["late_count"] += 1
    u["total_minutes"] += minutes
    return minutes


def _build_dept_ranking(per_dept_total: Dict[str, int], per_dept_late: Dict[str, int], depts: Dict[str, str]) -> List[Dict[str, Any]]:
    dept_ranking = []
    for dept_id, total in per_dept_total.items():
        late = per_dept_late.get(dept_id, 0)
        dept_ranking.append({
            "department_id": None if dept_id == "__none" else dept_id,
            "department_name": depts.get(dept_id, "Sin departamento"),
            "total_ins": total,
            "late": late,
            "late_pct": round((late / total) * 100, 1) if total else 0,
        })
    dept_ranking.sort(key=lambda x: x["late_pct"], reverse=True)
    return dept_ranking


@api.get("/stats/executive")
async def stats_executive(days: int = 30,
                          user: Dict[str, Any] = Depends(require_roles("admin", "coordinador", "gerente", "director"))) -> Dict[str, Any]:
    """Métricas ejecutivas: top tardanzas, ranking por depto, promedio minutos tarde."""
    since = now_utc() - timedelta(days=days)
    q: Dict[str, Any] = {"timestamp": {"$gte": since}, "type": "in"}
    scope_users_q: Dict[str, Any] = {}

    if user["role"] in LEADER_ROLES:
        team_ids = await supervisor_scope_ids(user)
        q["user_id"] = {"$in": team_ids}
        scope_users_q = {"user_id": {"$in": team_ids}}

    users = {u["user_id"]: u async for u in db.users.find(scope_users_q, {
        "user_id": 1, "name": 1, "email": 1, "cedula": 1,
        "department_id": 1, "position": 1, "picture": 1, "_id": 0,
    })}
    depts = {d["department_id"]: d["name"] async for d in db.departments.find({}, {"department_id": 1, "name": 1, "_id": 0})}

    per_user: Dict[str, Dict[str, Any]] = {}
    per_dept_late: Dict[str, int] = {}
    per_dept_total: Dict[str, int] = {}
    total_ins = total_late = total_late_minor = total_late_major = total_late_major_pending = total_late_minutes = 0

    async for r in db.attendance.find(q, {
        "user_id": 1, "is_late": 1, "late_minutes": 1, "late_severity": 1,
        "requires_justification": 1, "justification": 1, "justification_status": 1, "_id": 0,
    }):
        total_ins += 1
        uid = r.get("user_id", "")
        u_info = users.get(uid, {})
        dept_id = u_info.get("department_id") or "__none"
        per_dept_total[dept_id] = per_dept_total.get(dept_id, 0) + 1

        if not r.get("is_late") or r.get("justification_status") == "approved":
            continue

        total_late += 1
        total_late_minutes += _process_late_record(r, u_info, depts, per_user)
        per_dept_late[dept_id] = per_dept_late.get(dept_id, 0) + 1

        sev = r.get("late_severity") or ("late_major" if int(r.get("late_minutes") or 0) > 30 else "late_minor")
        if sev == "late_major":
            total_late_major += 1
            if (r.get("justification_status") or "none") in ("none", "pending"):
                total_late_major_pending += 1
        else:
            total_late_minor += 1

    top_late = sorted(per_user.values(), key=lambda x: (x["late_count"], x["total_minutes"]), reverse=True)[:5]
    dept_ranking = _build_dept_ranking(per_dept_total, per_dept_late, depts)

    return {
        "days": days,
        "since": since.isoformat(),
        "generated_at": now_utc().isoformat(),
        "total_check_ins": total_ins,
        "total_late": total_late,
        "total_late_minor": total_late_minor,
        "total_late_major": total_late_major,
        "total_late_major_pending": total_late_major_pending,
        "late_pct": round((total_late / total_ins) * 100, 1) if total_ins else 0,
        "avg_late_minutes": round(total_late_minutes / total_late, 1) if total_late else 0,
        "top_late": top_late,
        "department_ranking": dept_ranking[:8],
    }


@api.get("/stats/dashboard")
async def stats_dashboard(user: Dict[str, Any] = Depends(require_roles("admin", "coordinador", "gerente", "director"))) -> Dict[str, Any]:
    since = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    q: Dict[str, Any] = {"timestamp": {"$gte": since}}
    if user["role"] in LEADER_ROLES:
        team_ids = await supervisor_scope_ids(user)
        q["user_id"] = {"$in": team_ids}

    ins = await db.attendance.count_documents({**q, "type": "in"})
    outs = await db.attendance.count_documents({**q, "type": "out"})
    late = await db.attendance.count_documents({**q, "type": "in", "is_late": True, "justification_status": {"$ne": "approved"}})

    novelties_q: Dict[str, Any] = {"status": "approved"}
    today_str = since.strftime("%Y-%m-%d")
    novelties_q["start_date"] = {"$lte": today_str}
    novelties_q["end_date"] = {"$gte": today_str}
    if user["role"] in LEADER_ROLES:
        team_ids = await supervisor_scope_ids(user)
        novelties_q["user_id"] = {"$in": team_ids}

    active_novelties = await db.novelties.count_documents(novelties_q)

    # NUEVO: Calcular empleados totales y empleados con rostro registrado.
    user_q: Dict[str, Any] = {}
    if user["role"] in LEADER_ROLES:
        team_ids = await supervisor_scope_ids(user)
        user_q["user_id"] = {"$in": team_ids}

    total_users = await db.users.count_documents(user_q)
    onboarded_users = await db.users.count_documents({**user_q, "onboarded": True})

    return {
        "date": today_str,
        "check_ins_today": ins,
        "check_outs_today": outs,
        "late_today": late,
        "active_novelties_today": active_novelties,
        "total_users": total_users,
        "onboarded_users": onboarded_users,
    }

@api.get("/reports")
async def reports_list(from_date: Optional[str] = Query(None),
                       to_date: Optional[str] = Query(None),
                       user_id: Optional[str] = Query(None),
                       site_id: Optional[str] = Query(None),
                       user: Dict[str, Any] = Depends(get_current_user)) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {}
    if user_id:
        q["user_id"] = user_id
    if site_id:
        q["site_id"] = site_id
    rng = _parse_date_range(from_date, to_date)
    if rng:
        q["timestamp"] = rng
    if user["role"] == "employee":
        q["user_id"] = user["user_id"]
    elif user["role"] in LEADER_ROLES:
        team_ids = await supervisor_scope_ids(user)
        if user_id and user_id not in team_ids:
            return []
        q["user_id"] = {"$in": team_ids} if not user_id else user_id
    docs = await db.attendance.find(q, {"selfie_base64": 0}).sort("timestamp", -1).limit(5000).to_list(5000)
    return [strip_mongo_id(d) for d in docs]


_SEVERITY_ES = {"on_time": "A tiempo", "late_minor": "Tardanza leve", "late_major": "Tardanza grave"}
_JUST_STATUS_ES = {"none": "Sin justificación", "pending": "Pendiente", "approved": "Aprobada", "rejected": "Rechazada"}


@api.get("/reports/export")
async def reports_export(from_date: Optional[str] = Query(None),
                         to_date: Optional[str] = Query(None),
                         user_id: Optional[str] = Query(None),
                         site_id: Optional[str] = Query(None),
                         department_id: Optional[str] = Query(None),
                         type: Optional[str] = Query(None),
                         status: Optional[str] = Query(None),
                         user: Dict[str, Any] = Depends(get_current_user)) -> StreamingResponse:
    q: Dict[str, Any] = {}
    rng = _parse_date_range(from_date, to_date)
    if rng:
        q["timestamp"] = rng
    if site_id:
        q["site_id"] = site_id
    if type in ("in", "out"):
        q["type"] = type
    if status == "late":
        q["is_late"] = True
    elif status == "ontime":
        q["type"] = "in"
        q["is_late"] = {"$ne": True}
    allowed: Optional[set] = None
    if user["role"] == "employee":
        allowed = {user["user_id"]}
    elif user["role"] in LEADER_ROLES:
        allowed = set(await supervisor_scope_ids(user))
    if user_id:
        allowed = {user_id} if allowed is None or user_id in allowed else set()
    if department_id:
        dept_ids = {u["user_id"] async for u in db.users.find({"department_id": department_id}, {"user_id": 1})}
        allowed = dept_ids if allowed is None else allowed & dept_ids
    if allowed is not None:
        q["user_id"] = {"$in": list(allowed)}
    users = {u["user_id"]: u async for u in db.users.find({}, {"user_id": 1, "name": 1, "cedula": 1})}
    yes_no = lambda v: "Sí" if v else "No"  # noqa: E731
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM: Excel abre correctamente tildes y eñes
    w = csv.writer(buf)
    w.writerow(["Nombre", "Cédula", "Tipo", "Fecha y hora", "Tardanza", "Minutos de tardanza",
                "Severidad", "Requiere justificación", "Justificación",
                "Estado de justificación", "Motivo de rechazo"])
    async for r in db.attendance.find(q).sort("timestamp", -1):
        u = users.get(r.get("user_id"), {})
        ts = r.get("timestamp")
        ts_local = ts.astimezone(APP_TZ).strftime("%d/%m/%Y %H:%M:%S") if isinstance(ts, datetime) else str(ts or "")
        w.writerow([u.get("name") or "", u.get("cedula") or "",
                    "Entrada" if r.get("type") == "in" else "Salida", ts_local,
                    yes_no(r.get("is_late")), r.get("late_minutes") or 0,
                    _SEVERITY_ES.get(r.get("late_severity") or "", ""),
                    yes_no(r.get("requires_justification")),
                    r.get("justification") or "",
                    _JUST_STATUS_ES.get(r.get("justification_status") or "none", r.get("justification_status")),
                    r.get("rejection_reason") or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=asistencia_report.csv"})
