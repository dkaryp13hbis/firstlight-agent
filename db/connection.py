import pyodbc
import config


def connect_mssql(host: str, port: int, user: str, password: str,
                  database: str = "bidata",
                  driver: str = "ODBC Driver 18 for SQL Server",
                  timeout: int = 30) -> pyodbc.Connection:
    """Connect to a SQL Server by explicit address — used by the Railway
    tunnel-direct path (host is a local cloudflared port). Encryption is
    disabled: on-prem Protel/Pylon SQL Servers typically lack TLS certs, and
    the Cloudflare tunnel already encrypts the wire."""
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        f"Encrypt=no;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=timeout)


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
