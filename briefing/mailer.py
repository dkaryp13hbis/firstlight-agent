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

_DARK_NUM = re.compile(r'[+\-−]?€?\d(?:[\d,]*\d)?(?:\.\d+)?(?:[KkMm]\b)?%?')


def _highlight_dark(text: str) -> str:
    """Bold numbers for dark surfaces (the hero): € white, +% mint, −% coral.
    SINGLE regex pass — chained substitutions would re-match the digits inside
    the inserted hex colours (#56FFC4 → '56') and shred the markup."""
    def repl(m):
        tok = m.group(0)
        if tok.endswith('%') and tok.startswith('+'):
            return f'<strong style="color:#56FFC4">{tok}</strong>'
        if tok.endswith('%') and tok[0] in '-−':
            return f'<strong style="color:#FF9B8A">{tok}</strong>'
        return f'<strong style="color:#fff">{tok}</strong>'
    return _DARK_NUM.sub(repl, text)


_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _render(data: dict[str, Any], ai: dict[str, Any],
            charts: dict[str, Any] | None = None) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
    )
    env.filters["euro"]      = lambda v: f"€{v:,.0f}".replace(",", ".")
    env.filters["pct"]       = lambda v: f"{v * 100:.1f}%"
    env.filters["kilo"]      = lambda v: (f"€{v/1000:.1f}k" if v >= 1000 else f"€{int(v)}")
    env.filters["kilo0"]     = lambda v: (f"€{round(v/1000)}k" if v >= 1000 else f"€{int(v)}")
    env.filters["euro_full"] = lambda v: f"€{int(round(v)):,}"
    env.filters["highlight"] = _highlight
    env.filters["highlight_dark"] = _highlight_dark

    # Closed months (fully in the past) show STLY only in the occupancy chart —
    # their Final LY equals STLY, so drawing both duplicates one value.
    from datetime import date as _d
    env.globals["current_month_num"] = _d.today().month
    # Shown in the Smart Summary header as the LAST REFRESH date (renders
    # happen at refresh time, so render date == refresh date).
    env.globals["render_date"] = _d.today().strftime("%d %b").upper()

    return env.get_template("email.html").render(data=data, ai=ai,
                                                 charts=charts or {})


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
    """App-facing HTML: includes the mobile chart set. Email (send) does not —
    email clients cannot render the grid/flex layouts."""
    from briefing.charts import compute_chart_series
    html = _render(data, ai, charts=compute_chart_series(data))
    Path(path).write_text(html, encoding="utf-8")
    print(f"[mailer] Preview saved -> {path}")
