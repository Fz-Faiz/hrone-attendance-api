# Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance tracking and analytics, implemented for the HROne technical assignment.

## Features

- **Readiness Probe (`GET /health`)**: Verifies MongoDB connectivity via database ping; returns 503 on database unavailability.
- **Employee Management (`POST /employees`, `GET /employees`)**: Strict input validation, server-side UTC creation timestamp, database-enforced unique business code `emp_code`, and paginated listings.
- **Attendance Punch-In & Punch-Out (`POST /attendance/punch-in`, `POST /attendance/punch-out`)**:
  - Handles overnight shifts (R1) across midnight.
  - Computes late minutes with 10-minute grace period (R2).
  - Calculates overtime (R3) and work hours with half-up rounding (R4).
  - Determines half-day status (R5).
  - Atomic database constraints prevent concurrent duplicate punches.
- **Manual Attendance Regularization (`PATCH /attendance/{emp_code}/{date}`)**:
  - Validates permissible status transitions and time boundaries.
  - Recalculates dependent metrics.
  - Appends immutable audit history tracking field-level changes.
  - Optimistic concurrency control (OCC) prevents race overwrites.
- **Analytics & Leaderboard**:
  - Monthly employee attendance breakdown (`GET /analytics/employees/{emp_code}/monthly`).
  - Department summary with zero-log headcount retention (`GET /analytics/departments/summary`).
  - Late-comers leaderboard with standard competition ranking and tie handling (`GET /analytics/leaderboard/late`).
  - Daily department attendance trend with calendar gap-filling via `$densify` and 7-day moving average (`GET /analytics/departments/{department}/trend`).
- **Query Optimization & Verification (`GET /admin/explain/{endpoint}`)**:
  - Detailed `executionStats` query plans.
  - All operations utilize index scans (`IXSCAN`) with zero collection scans (`COLLSCAN`).

## Requirements

- Python 3.11+
- MongoDB 6.0+

## Local Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables (optional):**
   ```bash
   # Defaults:
   # MONGO_URI="mongodb://localhost:27017"
   # MONGO_DB="attendance_db"
   ```

3. **Run the application:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

4. **Run test suite:**
   ```bash
   pytest candidate_kit/tests
   ```

## Repository Structure

- `app/main.py`: Complete FastAPI application implementation.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Starter code defect audit and resolutions.
- `DECISIONS.md`: Key architectural decisions and trade-offs.
- `README.md`: Project overview and run instructions.
