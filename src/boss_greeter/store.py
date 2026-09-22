"""SQLite 持久化：岗位快照、招呼语与投递结果、拒绝原因统计、每轮运行记录。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .models import Greeting, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    company      TEXT,
    url          TEXT,
    salary_text  TEXT,
    area_text    TEXT,
    experience   TEXT,
    education    TEXT,
    company_size TEXT,
    company_stage TEXT,
    activity     TEXT,
    jd           TEXT,
    keyword      TEXT,
    scraped_at   TEXT
);

CREATE TABLE IF NOT EXISTS greetings (
    job_id   TEXT PRIMARY KEY,
    message  TEXT NOT NULL,
    model    TEXT,
    status   TEXT NOT NULL,       -- sent | already | failed | dry_run | skipped
    reason   TEXT,
    sent_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_greetings_status_time ON greetings(status, sent_at);

CREATE TABLE IF NOT EXISTS rejections (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER,
    job_id  TEXT,
    rule    TEXT NOT NULL,
    reason  TEXT,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_rule_time ON rejections(rule, at);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    scanned     INTEGER DEFAULT 0,
    rejected    INTEGER DEFAULT 0,
    sent        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    dry_run     INTEGER DEFAULT 0,
    stop_reason TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 岗位

    def save_job(self, job: Job) -> None:
        self.conn.execute(
            """INSERT INTO jobs (job_id, title, company, url, salary_text, area_text,
                                 experience, education, company_size, company_stage,
                                 activity, jd, keyword, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 jd = excluded.jd, activity = excluded.activity,
                 scraped_at = excluded.scraped_at""",
            (job.job_id, job.title, job.company, job.url, job.salary_text, job.area_text,
             job.experience, job.education, job.company_size, job.company_stage,
             job.activity, job.jd, job.keyword, job.scraped_at),
        )
        self.conn.commit()

    # ------------------------------------------------------------ 招呼语

    def is_greeted(self, job_id: str) -> bool:
        """打过招呼就不再投。dry_run / failed 不挡住后续重试。

        'already' 也算打过——它是详情页按钮已经是「继续沟通」，说明这个 HR
        之前就聊过了（本工具发的，或你自己手动发的）。不挡住的话每轮都会
        重新进详情页、重新点一次，纯属白跑。
        """
        row = self.conn.execute(
            "SELECT 1 FROM greetings WHERE job_id = ? AND status IN ('sent', 'already')",
            (job_id,),
        ).fetchone()
        return row is not None

    def save_greeting(self, g: Greeting) -> None:
        self.conn.execute(
            """INSERT INTO greetings (job_id, message, model, status, reason, sent_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 message = excluded.message, model = excluded.model,
                 status = excluded.status, reason = excluded.reason,
                 sent_at = excluded.sent_at""",
            (g.job_id, g.message, g.model, g.status, g.reason, g.sent_at),
        )
        self.conn.commit()

    def sent_today(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM greetings WHERE status = 'sent' AND date(sent_at) = ?",
            (date.today().isoformat(),),
        ).fetchone()
        return row["n"]

    # ------------------------------------------------------------ 拒绝统计

    def record_rejection(self, run_id: int | None, job_id: str, rule: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO rejections (run_id, job_id, rule, reason, at) VALUES (?,?,?,?,?)",
            (run_id, job_id, rule, reason, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def rejection_counts(self, days: int = 7) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT rule, COUNT(*) AS n FROM rejections
               WHERE at >= date('now', ?) GROUP BY rule ORDER BY n DESC""",
            (f"-{days} days",),
        ).fetchall()

    # ------------------------------------------------------------ 运行记录

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, scanned: int, rejected: int, sent: int,
                   failed: int, dry_run: int, stop_reason: str) -> None:
        self.conn.execute(
            """UPDATE runs SET ended_at = ?, scanned = ?, rejected = ?, sent = ?,
                               failed = ?, dry_run = ?, stop_reason = ? WHERE id = ?""",
            (datetime.now().isoformat(timespec="seconds"), scanned, rejected, sent,
             failed, dry_run, stop_reason, run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def greeting_counts(self, days: int = 7) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT status, COUNT(*) AS n FROM greetings
               WHERE sent_at >= date('now', ?) GROUP BY status ORDER BY n DESC""",
            (f"-{days} days",),
        ).fetchall()

    def recent_greetings(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT g.sent_at, g.status, g.message, j.title, j.company
               FROM greetings g LEFT JOIN jobs j ON j.job_id = g.job_id
               ORDER BY g.sent_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


@contextmanager
def open_store(db_path: Path):
    store = Store(db_path)
    try:
        yield store
    finally:
        store.close()
