#!/usr/bin/env python3
"""Create the EMPTY UAT database on the existing RDS instance. Safe to re-run.
Uses the old EC2 .env only to log in; it never reads or changes any existing database.

    python3 ~/nurseconnect-uat/deploy/uat/create_uat_db.py
"""
import os

from sqlalchemy import create_engine, text

SRC = os.path.expanduser("~/nurseconnect--backend/.env")
url = [l.split("=", 1)[1].strip() for l in open(SRC) if l.startswith("DATABASE_URL_SYNC=")][0]
engine = create_engine(url, isolation_level="AUTOCOMMIT")
with engine.connect() as c:
    if c.execute(text("select 1 from pg_database where datname = 'nurseconnect_uat'")).scalar():
        print("nurseconnect_uat already exists: nothing to do")
    else:
        c.execute(text("create database nurseconnect_uat"))
        print("created database nurseconnect_uat")
