"""Employee Attendance & Analytics API."""
import os, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId, json_util

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def parse_epoch(ms: Optional[int], field="punched_at"):
    if ms is None: return now_utc()
    try:
        # Reject absurd/unrepresentable timestamps as request validation failures.
        return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError, TypeError):
        raise HTTPException(422, f"invalid {field}")

def epoch(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def instant(value):
    if value is None: return None
    if value.tzinfo is None: return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

def money_round(n, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(n)).quantize(quantum, rounding=ROUND_HALF_UP))

def dt_local(value):
    return instant(value).astimezone(IST)

def attendance_date(punch_in, emp):
    local = dt_local(punch_in)
    shs = time.fromisoformat(emp["shift_start"])
    she = time.fromisoformat(emp["shift_end"])
    d = local.date()
    if she <= shs and local.time().replace(tzinfo=None) < she:
        d -= timedelta(days=1)
    return d.isoformat()

def shift_start_instant(day, emp):
    d = date.fromisoformat(day)
    shs = time.fromisoformat(emp["shift_start"])
    return datetime.combine(d, shs, IST).astimezone(UTC)

def shift_end_instant(day, emp):
    d = date.fromisoformat(day)
    shs, she = time.fromisoformat(emp["shift_start"]), time.fromisoformat(emp["shift_end"])
    if she <= shs: d += timedelta(days=1)
    return datetime.combine(d, she, IST).astimezone(UTC)

def compute_late_minutes(punch_in, shift_start):
    pi = dt_local(punch_in)
    start = pi.replace(hour=int(shift_start[:2]), minute=int(shift_start[3:]), second=0, microsecond=0)
    delta = (pi - start).total_seconds()
    return int(delta // 60) if delta > 600 else 0

def compute_work_hours(punch_in, punch_out):
    return money_round((instant(punch_out)-instant(punch_in)).total_seconds()/3600, 2)

def compute_overtime(punch_out, shift_end, date_str, shift_start=None):
    # Caller supplies record attendance date; overnight shift end is next day.
    d = date.fromisoformat(date_str)
    if shift_start and time.fromisoformat(shift_end) <= time.fromisoformat(shift_start):
        d += timedelta(days=1)
    end = datetime.combine(d, time.fromisoformat(shift_end), IST).astimezone(UTC)
    delta = int((instant(punch_out)-end).total_seconds() // 60)
    return delta if delta >= 30 else 0

def derive_fields(doc, emp):
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if doc["status"] in ("ABSENT", "LEAVE"):
        doc.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
        return doc
    if pi is None: raise HTTPException(422, "presence status requires punch_in")
    doc["late_minutes"] = compute_late_minutes(pi, emp["shift_start"])
    if po is None:
        doc.update(work_hours=None, overtime_minutes=0, half_day=False)
    else:
        doc["work_hours"] = compute_work_hours(pi, po)
        doc["overtime_minutes"] = compute_overtime(po, emp["shift_end"], doc["date"], emp["shift_start"])
        doc["half_day"] = doc["work_hours"] < 4.50
    return doc

def out_doc(doc):
    if doc is None: return None
    result = dict(doc)
    result.pop("_id", None)
    for key in ("punch_in", "punch_out", "created_at"):
        if key in result: result[key] = epoch(result[key])
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    for h in result["history"]:
        h["at"] = epoch(h.get("at"))
        for ch in h.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(ch.get(side), datetime): ch[side] = epoch(ch[side])
    return result

def valid_date(s):
    try: return date.fromisoformat(s).isoformat() == s
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")

def month_range(month):
    try:
        d = datetime.strptime(month, "%Y-%m").date()
        if d.strftime("%Y-%m") != month: raise ValueError()
    except (ValueError, TypeError): raise HTTPException(422, "month must be YYYY-MM")
    end = date(d.year, d.month, calendar.monthrange(d.year,d.month)[1])
    return d.isoformat(), end.isoformat(), d, end

def working_days(start, end):
    if start > end: return 0
    n = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5: n += 1
        cur += timedelta(days=1)
    return n

def epoch_fields_pipeline():
    return [{"$addFields": {"_id": {"$toString":"$_id"}}}]

@app.on_event("startup")
def startup():
    # All indexes are idempotent and support the list, natural-key, and analytics queries.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_status_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")

class EmployeeIn(BaseModel):
    emp_code: str = Field(min_length=7, max_length=9, pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_time(cls, v):
        try:
            if len(v) != 5 or time.fromisoformat(v).strftime("%H:%M") != v: raise ValueError()
        except ValueError: raise ValueError("must be HH:MM")
        return v
    @field_validator("joined_on")
    @classmethod
    def valid_join_date(cls,v):
        date.fromisoformat(v)
        if len(v)!=10: raise ValueError("must be YYYY-MM-DD")
        return v
    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None
    status: Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status":"ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return out_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department":department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id":0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size))
    return {"items":[out_doc(x) for x in items], "total":total, "page":page, "page_size":page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = parse_epoch(body.punched_at)
    d = attendance_date(ts, emp)
    doc = {"emp_code":body.emp_code, "date":d, "status":body.status, "punch_in":ts, "punch_out":None,
           "work_hours":None, "late_minutes":compute_late_minutes(ts,emp["shift_start"]), "overtime_minutes":0,
           "half_day":False, "history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return out_doc(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = parse_epoch(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code":body.emp_code, "punch_in":{"$lte":ts}}, sort=[("punch_in",DESCENDING)])
    if not doc: raise HTTPException(404, "no punch-in found")
    pi = instant(doc["punch_in"])
    if ts <= pi or (ts-pi) > timedelta(hours=24): raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    updated = db.attendance_logs.find_one_and_update(
        {"_id":doc["_id"], "punch_out":None, "punch_in":{"$lte":ts}},
        {"$set":{"punch_out":ts, "work_hours":compute_work_hours(pi,ts),
                 "overtime_minutes":compute_overtime(ts,emp["shift_end"],doc["date"],emp["shift_start"]),
                 "half_day":compute_work_hours(pi,ts)<4.5}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        latest = db.attendance_logs.find_one({"_id":doc["_id"]})
        if latest and latest.get("punch_out") is not None: raise HTTPException(409, "already punched out")
        raise HTTPException(409, "record changed concurrently")
    return out_doc(updated)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str]=None, date_from: Optional[str]=None, date_to: Optional[str]=None,
                    status: Optional[str]=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    if date_from: valid_date(date_from)
    if date_to: valid_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    q={}
    if emp_code is not None: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from: q["date"]["$gte"]=date_from
        if date_to: q["date"]["$lte"]=date_to
    if status is not None: q["status"]=status
    total=db.attendance_logs.count_documents(q)
    docs=list(db.attendance_logs.find(q,{"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size))
    return {"items":[out_doc(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date_str = date
    valid_date(date_str)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date_str})
    if not old: raise HTTPException(404,"attendance record not found")
    new=dict(old)
    new["status"]=body.status if body.status is not None else old["status"]
    if body.status in ("ABSENT","LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
    if body.punch_in is not None: new["punch_in"]=parse_epoch(body.punch_in,"punch_in")
    if body.punch_out is not None: new["punch_out"]=parse_epoch(body.punch_out,"punch_out")
    if new["status"] in ("ABSENT","LEAVE"):
        new.update(punch_in=None,punch_out=None)
    else:
        if new.get("punch_in") is None: raise HTTPException(422,"presence status requires punch_in")
        if attendance_date(new["punch_in"],emp)!=date_str: raise HTTPException(422,"punch_in must belong to record date")
        if new.get("punch_out") is not None:
            delta=instant(new["punch_out"])-instant(new["punch_in"])
            if delta<=timedelta(0) or delta>timedelta(hours=24): raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")
    new=derive_fields(new,emp)
    keys=("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")
    changes={}
    defaults={"late_minutes":0,"overtime_minutes":0,"half_day":False,"work_hours":None,"punch_in":None,"punch_out":None}
    for k in keys:
        before=old.get(k,defaults.get(k))
        after=new.get(k,defaults.get(k))
        a=instant(before) if isinstance(before,datetime) else before
        b=instant(after) if isinstance(after,datetime) else after
        if a!=b:
            changes[k]={"from":before,"to":after}
    if not changes: raise HTTPException(422,"request changes nothing")
    # Atomic compare-and-set prevents concurrent regularizations from overwriting audit entries.
    old_hist=old.get("history",[])
    entry={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    set_fields={k:new.get(k) for k in keys}
    selector={"_id":old["_id"]}
    selector["history"]=old_hist if "history" in old else {"$exists":False}
    res=db.attendance_logs.update_one(selector, {"$set":set_fields,"$push":{"history":entry}})
    if res.modified_count != 1: raise HTTPException(409,"record changed concurrently; retry")
    return out_doc(db.attendance_logs.find_one({"_id":old["_id"]}))

def monthly_pipeline(emp_code, start, end):
    return [
        {"$match":{"emp_code":emp_code,"date":{"$gte":start,"$lte":end}}},
        {"$group":{"_id":None,
          "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$not":[{"$eq":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},1]}]},{"$ne":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},7]}]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
          "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
          "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},
          "total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
          "total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    start,end,month_start,month_end=month_range(month)
    wd_start=max(month_start,date.fromisoformat(emp["joined_on"]))
    wd=working_days(wd_start,month_end)
    got=list(db.attendance_logs.aggregate(monthly_pipeline(emp_code,start,end)))
    agg=got[0] if got else {}
    present=agg.get("present_days",0)
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":money_round(present),
      "leave_days":agg.get("leave_days",0),"late_count":agg.get("late_count",0),
      "total_late_minutes":agg.get("total_late_minutes",0),"total_overtime_minutes":agg.get("total_overtime_minutes",0),
      "attendance_pct":money_round(present/wd*100,4) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str]=None):
    start,end,month_start,month_end=month_range(month)
    empmatch={"joined_on":{"$lte":end}}
    if department is not None: empmatch["department"]=department
    pipeline=[
      {"$match":empmatch},
      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start]},{"$lte":["$date",end]}]}}}], "as":"logs"}},
      {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},
      {"$group":{"_id":"$department","headcount":{"$addToSet":"$emp_code"},
        "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$not":[{"$in":[{"$dayOfWeek":{"$dateFromString":{"dateString":{"$ifNull":["$logs.date",start]}}}},[1,7]]}]}]},{"$cond":[{"$eq":[{"$ifNull":["$logs.half_day",False]},True]},0.5,1]},0]}},
        "avg_work_hours":{"$avg":{"$cond":[{"$and":[{"$in":["$logs.status",list(PRESENCE)]},{"$ne":[{"$ifNull":["$logs.work_hours",None]},None]}]},"$logs.work_hours",None]}},
        "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$logs.late_minutes",0]},0]},1,0]}},
        "total_late_minutes":{"$sum":{"$ifNull":["$logs.late_minutes",0]}},
        "leave_count":{"$sum":{"$cond":[{"$eq":["$logs.status","LEAVE"]},1,0]}},
        "on_duty_count":{"$sum":{"$cond":[{"$eq":["$logs.status","ON_DUTY"]},1,0]}}}},
      {"$project":{"_id":0,"department":"$_id","headcount":{"$size":"$headcount"},"present_days":1,
        "avg_work_hours":1,
        "late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
      {"$sort":{"department":1}}]
    rows=list(db.employees.aggregate(pipeline))
    return {"month":month,"items":[{"department":r["department"],"headcount":r["headcount"],"present_days":money_round(r["present_days"]),
      "avg_work_hours":money_round(r["avg_work_hours"]) if r.get("avg_work_hours") is not None else None,
      "late_count":r["late_count"],"total_late_minutes":r["total_late_minutes"],"leave_count":r["leave_count"],"on_duty_count":r["on_duty_count"]} for r in rows if r["headcount"]>0]}

def leaderboard_pipeline(start,end,department=None,limit=10):
    pipeline=[{"$match":{"date":{"$gte":start,"$lte":end},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$match":{"total_late_minutes":{"$gt":0}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},
      {"$unwind":"$employee"}]
    if department is not None: pipeline.append({"$match":{"employee.department":department}})
    pipeline += [{"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}},
      {"$sort":{"total_late_minutes":-1,"emp_code":1}}]
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int=Query(10,ge=1,le=50), department: Optional[str]=None):
    start,end,_,_=month_range(month)
    return {"month":month,"items":list(db.attendance_logs.aggregate(leaderboard_pipeline(start,end,department,limit)))}

def trend_pipeline(department,start,end):
    startdt=datetime.combine(date.fromisoformat(start),time.min,IST).astimezone(UTC)
    enddt=datetime.combine(date.fromisoformat(end),time.min,IST).astimezone(UTC)
    return [
      {"$documents":[{"day":startdt}]},
      {"$densify":{"field":"day","range":{"step":1,"unit":"day","bounds":[startdt,enddt+timedelta(days=1)]}}},
      {"$set":{"date_str":{"$dateToString":{"date":"$day","format":"%Y-%m-%d","timezone":"+05:30"}}}},
      {"$lookup":{"from":"employees","let":{"d":"$date_str"},"pipeline":[{"$match":{"department":department}},{"$match":{"$expr":{"$lte":["$joined_on","$$d"]}}},{"$count":"n"}],"as":"hc"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]}}},
      {"$lookup":{"from":"attendance_logs","let":{"d":"$date_str"},"pipeline":[
        {"$match":{"$expr":{"$eq":["$date","$$d"]}}},
        {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},
        {"$unwind":"$employee"},{"$match":{"employee.department":department}},
        {"$group":{"_id":None,"present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}}
      ],"as":"daily"}},
      {"$set":{"present_count":{"$ifNull":[{"$arrayElemAt":["$daily.present_count",0]},0]},"late_count":{"$ifNull":[{"$arrayElemAt":["$daily.late_count",0]},0]},
        "is_working_day":{"$and":[{"$gte":[{"$dayOfWeek":{"date":"$day","timezone":"+05:30"}},2]},{"$lte":[{"$dayOfWeek":{"date":"$day","timezone":"+05:30"}},6]}]}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$divide":[{"$floor":{"$add":[{"$multiply":[{"$divide":["$present_count","$headcount"]},10000]},0.5]}},10000]},None]}}},
      {"$setWindowFields":{"sortBy":{"day":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":"$date_str","is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$cond":[{"$eq":[{"$type":"$moving_avg_7d"},"missing"]},None,{"$divide":[{"$floor":{"$add":[{"$multiply":["$moving_avg_7d",10000]},0.5]}},10000]}]}}},
      {"$sort":{"date":1}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str=Query(alias="from"), to_date: str=Query(alias="to")):
    valid_date(from_date); valid_date(to_date)
    d1,d2=date.fromisoformat(from_date),date.fromisoformat(to_date)
    if d2<d1 or (d2-d1).days+1>92: raise HTTPException(422,"invalid date range")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    # Collectionless aggregation starts from a seed date, densifies the calendar, and joins daily facts.
    pipeline=trend_pipeline(department,from_date,to_date)
    rows=list(db.command({"aggregate":1,"pipeline":pipeline,"cursor":{}})["cursor"]["firstBatch"])
    return {"department":department,"items":rows}

def explain_command(collection, pipeline=None, query=None, sort=None, skip=0, limit=None):
    if pipeline is not None:
        cmd={"aggregate":collection,"pipeline":pipeline,"cursor":{}}
    else:
        cmd={"find":collection,"filter":query or {}}
        if sort: cmd["sort"]={k:v for k,v in sort}
        cmd["skip"]=skip
        if limit is not None: cmd["limit"]=limit
    return client[db.name].command({"explain":cmd,"verbosity":"executionStats"})

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str]=None, month: Optional[str]=None, department: Optional[str]=None,
                     limit: int=Query(10,ge=1,le=50), date_from: Optional[str]=None,date_to: Optional[str]=None,status: Optional[str]=None,
                     from_date: Optional[str]=Query(None,alias="from"),to_date: Optional[str]=Query(None,alias="to"),
                     page: int=Query(1,ge=1),page_size: int=Query(20,ge=1,le=100)):
    if endpoint=="attendance_list":
        q={}
        if emp_code is not None:q["emp_code"]=emp_code
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=valid_date(date_from)
            if date_to:q["date"]["$lte"]=valid_date(date_to)
            if date_from and date_to and date_from>date_to:raise HTTPException(422,"invalid date range")
        if status:q["status"]=status
        ex=explain_command("attendance_logs",query=q,sort=[("date",-1),("emp_code",1)],skip=(page-1)*page_size,limit=page_size)
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="employee_monthly":
        if not emp_code or not month:raise HTTPException(422,"emp_code and month are required")
        start,end,_,_=month_range(month)
        ex=explain_command("attendance_logs",pipeline=monthly_pipeline(emp_code,start,end))
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="department_summary":
        if not month:raise HTTPException(422,"month is required")
        start,end,_,_=month_range(month)
        # Same employee-driven aggregation as the endpoint.
        em={"joined_on":{"$lte":end}}
        if department is not None:em["department"]=department
        ex=explain_command("employees",pipeline=[{"$match":em},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start]},{"$lte":["$date",end]}]}}}],"as":"logs"}}])
        return {"endpoint":endpoint,"collection":"employees","explain":ex}
    if endpoint=="late_leaderboard":
        if not month:raise HTTPException(422,"month is required")
        start,end,_,_=month_range(month)
        ex=explain_command("attendance_logs",pipeline=leaderboard_pipeline(start,end,department,limit))
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    if endpoint=="department_trend":
        if not department or not from_date or not to_date:raise HTTPException(422,"department, from and to are required")
        valid_date(from_date);valid_date(to_date)
        pipeline=trend_pipeline(department,from_date,to_date)
        ex=client[db.name].command({"explain":{"aggregate":1,"pipeline":pipeline,"cursor":{}},"verbosity":"executionStats"})
        return {"endpoint":endpoint,"collection":"attendance_logs","explain":ex}
    raise HTTPException(422,"unknown endpoint")
