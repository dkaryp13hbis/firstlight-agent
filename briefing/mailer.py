"""
Renders the Jinja2 email template and sends via Gmail SMTP.
Requires a Gmail App Password (not the account password).
Create one at: myaccount.google.com -> Security -> App passwords
"""

import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

import config


def _highlight(text: str) -> str:
    """Bold € amounts; color +X% green and -X% red in insight text."""
    # Positive variance: +X% or +X.X%
    text = re.sub(
        r'\+(\d+\.?\d*%)',
        r'<strong style="color:#1A7A50">+\1</strong>', text
    )
    # Negative variance: -X% or −X%
    text = re.sub(
        r'[-−](\d+\.?\d*%)',
        r'<strong style="color:#B83A1B">-\1</strong>', text
    )
    # Euro amounts: €X, €X,XXX, €X.Xk, €XM etc.
    text = re.sub(
        r'€([\d,\.]+\s*[KkMm]?)',
        r'<strong>€\1</strong>', text
    )
    return text

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _render(data: dict[str, Any], ai: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
    )
    env.filters["euro"]      = lambda v: f"€{v:,.0f}".replace(",", ".")
    env.filters["pct"]       = lambda v: f"{v * 100:.1f}%"
    env.filters["kilo"]      = lambda v: (f"€{v/1000:.1f}k" if v >= 1000 else f"€{int(v)}")
    env.filters["kilo0"]     = lambda v: (f"€{round(v/1000)}k" if v >= 1000 else f"€{int(v)}")
    env.filters["highlight"] = _highlight

    return env.get_template("email.html").render(data=data, ai=ai)


def _subject(data: dict[str, Any]) -> str:
    yd = data["yesterday"]
    arrow = "▲" if yd["revenue"] >= yd["revenueLY"] else "▼"
    ly = max(yd["revenueLY"], 1)
    pct = abs((yd["revenue"] - yd["revenueLY"]) / ly * 100)
    return (
        f"☀️ {data['hotel_name']} · {data['report_date']} · "
        f"€{yd['revenue']:,.0f} rev {arrow}{pct:.1f}%"
    )


def send(data: dict[str, Any], ai: dict[str, Any]) -> bool:
    html = _render(data, ai)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = _subject(data)
    msg["From"]    = f"{config.SENDER_NAME} <{config.SMTP_USER}>"
    msg["To"]      = config.RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_USER, config.RECIPIENT_EMAIL, msg.as_string())
        print(f"[mailer] Sent -> {config.RECIPIENT_EMAIL}")
        return True
    except Exception as exc:
        print(f"[mailer] SMTP error: {exc}")
        return False


def save_preview(data: dict[str, Any], ai: dict[str, Any], path: str = "preview.html") -> None:
    html = _render(data, ai)
    Path(path).write_text(html, encoding="utf-8")
    print(f"[mailer] Preview saved -> {path}")
