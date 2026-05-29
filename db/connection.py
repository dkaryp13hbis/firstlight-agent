import pyodbc
import config


def get_connection() -> pyodbc.Connection:
    if config.SQL_TRUSTED.lower() == "yes":
        conn_str = (
            f"DRIVER={{{config.SQL_DRIVER}}};"
            f"SERVER={config.SQL_SERVER};"
            f"DATABASE={config.SQL_DATABASE};"
            f"Trusted_Connection=yes;"
        )
    else:
        conn_str = (
            f"DRIVER={{{config.SQL_DRIVER}}};"
            f"SERVER={config.SQL_SERVER};"
            f"DATABASE={config.SQL_DATABASE};"
            f"UID={config.SQL_USER};"
            f"PWD={config.SQL_PASSWORD};"
        )
    return pyodbc.connect(conn_str, timeout=30)
