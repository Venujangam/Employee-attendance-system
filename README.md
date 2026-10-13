# Employee Attendance & Analytics API

FastAPI and PyMongo service for employee attendance, corrections, and analytics. Python 3.11+ and MongoDB 6.0+ are required.

## Setup (PowerShell)

From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Start a local MongoDB 7 instance with Docker if needed:

```powershell
docker run -d --name attendance-mongo -p 27017:27017 mongo:7
```

Set `MONGO_URI` and `MONGO_DB` in the environment, or put them in a local `.env` file. Environment variables take precedence.

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

The application creates its indexes at startup. Start it from the project root:

```powershell
python -m uvicorn app.main:app --port 8000 --reload
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Tests and sample data

With MongoDB reachable, run:

```powershell
python -m unittest discover -s tests -v
```

The tests use and drop a uniquely named temporary database. `python sample_seed.py` also loads the sample documents, but it first deletes all documents in the configured `employees` and `attendance_logs` collections; use a disposable database for seeding.

## Submission hygiene

Do not submit `.env`, `.venv`, credentials, or a Dockerfile. The `.gitignore` excludes local environment files and virtual environments. The assignment submission is a public Git repository containing the application, requirements, tests, and completed review/decision documents.
