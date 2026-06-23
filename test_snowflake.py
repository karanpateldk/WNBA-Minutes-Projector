"""Run this to verify Snowflake connectivity: double-click test_snowflake.bat"""
import os
import winreg

# Read PAT from Windows user environment registry (survives process restarts)
pat = os.getenv("SNOWFLAKE_PAT", "")
if not pat:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        pat, _ = winreg.QueryValueEx(key, "SNOWFLAKE_PAT")
    except Exception:
        pass

if not pat:
    print("ERROR: SNOWFLAKE_PAT not set.")
    print("Run in PowerShell: $env:SNOWFLAKE_PAT = 'your-token'")
    print("Then: [System.Environment]::SetEnvironmentVariable('SNOWFLAKE_PAT', $env:SNOWFLAKE_PAT, 'User')")
    input("\nPress Enter to exit...")
    raise SystemExit(1)

print(f"PAT found (length {len(pat)})")

import sys, tempfile, platform

# Windows App Store Python sandbox fix:
# The snowflake connector calls platform.libc_ver() which tries to open
# sys.executable as a binary — but the App Store python.exe is a stub that
# can't be opened. On Windows, libc_ver() is meaningless anyway (Linux only),
# so we patch it to return an empty result before importing the connector.
platform.libc_ver = lambda executable=None, lib='', version='', chunksize=16384: ('', '')

try:
    import snowflake.connector
except ImportError:
    print("Installing snowflake-connector-python...")
    import subprocess
    subprocess.check_call([_real_exe, "-m", "pip", "install", "snowflake-connector-python"])
    import snowflake.connector

print("Connecting to Snowflake...")
try:
    conn = snowflake.connector.connect(
        account="DRAFTKINGS-DRAFTKINGS",
        user="KAR.PATEL",
        authenticator="programmatic_access_token",
        token=pat,
        warehouse="QUERY_WH",
        database="SPORTRADAR",
        schema="DBO",
        insecure_mode=True,
    )
    # Print the active role so we know what was assigned
    cur2 = conn.cursor()
    cur2.execute("SELECT CURRENT_ROLE()")
    print(f"  Active role: {cur2.fetchone()[0]}")
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_USER(), CURRENT_WAREHOUSE(), CURRENT_DATABASE()")
    row = cur.fetchone()
    print(f"\nSUCCESS!")
    print(f"  User:      {row[0]}")
    print(f"  Warehouse: {row[1]}")
    print(f"  Database:  {row[2]}")

    cur.execute("SELECT COUNT(*) FROM SPORTRADAR.DBO.WNBA_SCHEDULE WHERE SEASON_YEAR = 2026 AND SEASON_TYPE = 'REG'")
    print(f"  2026 REG games in schedule: {cur.fetchone()[0]}")

    cur.execute("SELECT COUNT(*) FROM SPORTRADAR.DBO.WNBA_GAMESUMMARY_PLAYERS WHERE SCHEDULED >= '2026-05-16'")
    print(f"  2026 player-game rows:      {cur.fetchone()[0]}")

    conn.close()
    print("\nSnowflake integration is ready.")
except Exception as e:
    import traceback
    print(f"\nFAILED: {e}")
    print("\nFull traceback:")
    traceback.print_exc()

input("\nPress Enter to exit...")
