# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the supplied HROne candidate kit.

## Run

Python 3.11+ and MongoDB 6.0+ are required.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment (or a local `.env` file), then run from this directory:

```bash
uvicorn app.main:app --port 8000
```

`GET /health` pings MongoDB. Required indexes are created idempotently at application startup. To load the provided sample documents, run `python sample_seed.py` after configuring the environment.

## Files

- `app/main.py` — API endpoints, business rules, indexes, and MongoDB aggregation pipelines.
- `openapi.yaml` — source API contract.
- `DATA_MODEL.md` — storage conventions.
- `REVIEW.md` — starter-code review.
- `DECISIONS.md` — design trade-offs.

## Important notes

The sample dataset is only a shape example; it does not prove correctness for the hidden dataset. Run the app against MongoDB 6.0+ and exercise overnight shifts, tie ranks, no-log employees, weekend records, missing dates, and concurrent writes before submission. The department trend uses MongoDB's `$documents`, `$densify`, and `$setWindowFields` stages and therefore needs a compatible MongoDB version.
