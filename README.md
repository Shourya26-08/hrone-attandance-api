# Employee Attendance & Analytics API

Python 3.11+, FastAPI, and MongoDB 6.0+ (7.0 recommended). Run commands from
this directory, which should be the repository root. Application code is in
`app/main.py`; no external services other than MongoDB are used by the app.

## Run

Use a Python virtual environment, then install `requirements.txt`. Start a
local MongoDB server or supply an Atlas URI through environment variables.
For PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://127.0.0.1:27017"
$env:MONGO_DB = "attendance_db"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

On bash, create/activate a virtual environment with `python -m venv .venv` and
`source .venv/bin/activate`, then run `python -m pip install -r requirements.txt`
and `python -m uvicorn app.main:app --port 8000`. Use
`export MONGO_URI="mongodb://127.0.0.1:27017"` and
`export MONGO_DB="attendance_db"` instead of the two PowerShell assignments.
Set those variables before launching. The required launch command
`uvicorn app.main:app --port 8000` also works with the virtual environment activated.
A local `.env` is optional; real environment variables take precedence.
Never commit connection credentials.

Plain Uvicorn is sufficient; optional `standard` extras are not required. This
also avoids the PyYAML/C++ build requirement encountered on Windows/Python 3.15.

`GET /health` returns 200 after MongoDB responds and startup indexes are ready,
otherwise 503. No manual seeding or index creation is required. Browse
`http://127.0.0.1:8000/docs` to try the routes; the service intentionally has
no authentication, as specified by the exercise, so do not expose it publicly.

## Checks

No additional testing package is needed. Pure rule/validation tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_rules.py -v
```

Opt into the real MongoDB/HTTP checks (PowerShell):

```powershell
$env:RUN_MONGO_TESTS = "1"
$env:MONGO_TEST_URI = "mongodb://127.0.0.1:27017"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On bash with the virtual environment activated, use
`python -m unittest discover -s tests -p test_rules.py -v` for rules, and
`RUN_MONGO_TESTS=1 MONGO_TEST_URI="mongodb://127.0.0.1:27017" python -m unittest discover -s tests -v`
for the full suite.
Integration tests launch their own API process, create a random
`attendance_test_<uuid>` database and drop only that database afterward. Use
a test MongoDB account with permission to create databases/indexes. They do
not read or clear the configured application `MONGO_DB`. Without opt-in,
integration tests are explicitly skipped, not passed.

Coverage includes rounding/grace boundaries, overnight shifts, strict timestamps,
legacy fields, audit history, concurrent creates/punches/corrections, no-log
employees, mid-month joiners, weekends, ranking ties, trend gaps/windows, and
real explain plans. The integration fixture is small: it is not a 100k-record
performance benchmark.

**Verified locally on Windows with Python 3.15.0 and MongoDB 9.0.4:**

- Rule suite: **10 passed**.
- Full suite with real MongoDB/HTTP enabled: **23 passed**, no skips, in 5.591 s.
- All five explain targets used `IXSCAN` and had no `COLLSCAN` in the tested plans.
- The test API became ready within the integration harness's 20-second deadline.
- `python -m pip check`: no broken requirements.
- `python -m compileall -q app tests`: passed.

The full suite uses an isolated test database and a small fixture. Performance
at 100k records and behavior on MongoDB 7.0 (the grader version) or Python 3.11
have **not** been independently verified. Passing these checks does not prove
hidden-test correctness. `REVIEW.md` separates static starter findings from
these regression results.

## Submission contents

Keep `app/main.py`, `app/__init__.py`, `requirements.txt`, `README.md`,
`REVIEW.md`, `DECISIONS.md`, `.gitignore`, and optionally `tests/` at the
repository root. Supplied kit/reference files remain local and are ignored;
do not publish data dumps, `.env` files, credentials, a virtual environment,
or a Dockerfile. Review the staged file list before committing.
