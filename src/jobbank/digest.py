"""Weekly email digest (spec sec 8/13): new priority roles, urgent deadlines,
source failures. Sent via Resend if RESEND_API_KEY is set, else SMTP, else
written to out/digest.html (dry-run).
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx

from .models import Job
from .settings import Settings


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _eu_date(d) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def _score_colour(score: int | None) -> str:
    s = score or 0
    return "#0a7d18" if s >= 85 else "#2f7d32" if s >= 75 else "#8a6d00"


def _job_card(job: Job, today: date, sheet_url: str = "") -> str:
    """One self-contained card: everything needed to judge and apply, so the
    sheet never has to be opened."""
    days = (job.deadline - today).days if job.deadline else None
    if days is not None and days <= 7:
        deadline = (f"<span style='color:#c62828;font-weight:700'>Closes {_eu_date(job.deadline)}"
                    f" — {days} day{'s' if days != 1 else ''} left</span>")
    elif job.deadline:
        deadline = f"Closes {_eu_date(job.deadline)}"
    else:
        deadline = "<span style='color:#777'>No stated deadline</span>"

    bits = [b for b in (_esc(job.location), _esc(job.duration)) if b]
    meta = "  ·  ".join(bits)
    reason = _esc(job.score_reason)[:260]
    attain = _esc(job.attainability_note)[:180]
    flag = ("<span style='background:#ffd54f;color:#3b2f00;font-size:11px;font-weight:700;"
            "padding:2px 7px;border-radius:10px;margin-left:6px'>PRIORITY</span>"
            if job.priority else "")

    return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px 0;
       border:1px solid #dde3ea;border-radius:10px;border-collapse:separate">
 <tr><td style="padding:14px 16px">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
   <td style="font-size:22px;font-weight:800;color:{_score_colour(job.score)};
              width:52px;vertical-align:top">{job.score}</td>
   <td style="vertical-align:top">
     <div style="font-size:15px;font-weight:700;color:#0b1f33">
       {_esc(job.company)}{flag}</div>
     <div style="font-size:14px;margin:2px 0 6px 0">
       <a href="{_esc(job.url)}" style="color:#0b3d66;text-decoration:none">
         {_esc(job.title)} &rsaquo;</a></div>
     <div style="font-size:12px;color:#5f6b7a">{meta}</div>
     <div style="font-size:12px;margin-top:4px">{deadline}</div>
     <div style="font-size:12px;color:#37474f;margin-top:8px;line-height:1.45">{reason}</div>
     {f'<div style="font-size:11px;color:#78909c;margin-top:4px"><i>Odds: {attain}</i></div>' if attain else ''}
     <div style="margin-top:10px">
       <a href="{_esc(job.url)}" style="background:#0b3d66;color:#fff;font-size:12px;
          font-weight:700;padding:7px 14px;border-radius:6px;text-decoration:none">
          Open the posting</a></div>
   </td></tr></table>
 </td></tr></table>"""


def build_digest_html(added: list[Job], failures: dict[str, str],
                      stats_line: str, now_open: list[str] | None = None,
                      sheet_url: str = "", dashboard_url: str = "") -> str:
    today = date.today()
    ranked = sorted(added, key=lambda j: j.score or 0, reverse=True)
    urgent = [j for j in ranked if j.deadline and j.deadline <= today + timedelta(days=7)]

    now_open_html = ""
    if now_open:
        items = "".join(f"<li style='margin:3px 0'>{_esc(x)}</li>" for x in now_open)
        now_open_html = f"""
<div style="background:#e8f5e9;border-left:4px solid #0a7d18;padding:12px 16px;
     border-radius:6px;margin-bottom:18px">
  <div style="font-weight:800;color:#0a7d18;font-size:14px">
    &#9733; NOW OPEN — applications just went live</div>
  <ul style="margin:8px 0 0 18px;padding:0;font-size:13px;color:#1b3a1e">{items}</ul>
</div>"""

    urgent_html = ""
    if urgent:
        items = "".join(
            f"<li style='margin:3px 0'><a href='{_esc(j.url)}' style='color:#c62828'>"
            f"{_esc(j.company)} — {_esc(j.title)}</a> · {_eu_date(j.deadline)}</li>"
            for j in urgent)
        urgent_html = f"""
<div style="background:#ffebee;border-left:4px solid #c62828;padding:12px 16px;
     border-radius:6px;margin-bottom:18px">
  <div style="font-weight:800;color:#c62828;font-size:14px">
    &#9200; Closing within 7 days</div>
  <ul style="margin:8px 0 0 18px;padding:0;font-size:13px">{items}</ul>
</div>"""

    cards = ("".join(_job_card(j, today) for j in ranked) if ranked else
             "<p style='color:#888;font-size:13px'>No new roles cleared the bar this run.</p>")

    fail_html = ""
    if failures:
        items = "".join(f"<li><b>{_esc(s)}</b>: {_esc(e)[:140]}</li>"
                        for s, e in failures.items())
        fail_html = (f"<h3 style='font-size:13px;color:#8a6d00;margin-top:24px'>"
                     f"Source failures</h3><ul style='font-size:12px;color:#666'>{items}</ul>")

    dash_button = (
        f'<a href="{dashboard_url}" style="display:inline-block;margin-top:12px;'
        f'background:#fff;color:#0b1f33;font-size:12px;font-weight:700;padding:8px 15px;'
        f'border-radius:6px;text-decoration:none">Open the dashboard &rsaquo;</a>'
        if dashboard_url else "")
    sheet_link = ""
    if dashboard_url:
        sheet_link += f'<a href="{dashboard_url}" style="color:#0b3d66">Dashboard</a> &middot; '
    if sheet_url:
        sheet_link += f'<a href="{sheet_url}" style="color:#0b3d66">Spreadsheet</a> &middot; '

    return f"""<html><body style="margin:0;padding:24px;background:#f6f8fa;
     font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif">
<div style="max-width:680px;margin:0 auto">
  <div style="background:#0b1f33;color:#fff;padding:18px 20px;border-radius:10px 10px 0 0">
    <div style="font-size:19px;font-weight:800">Job Bank &mdash; {_eu_date(today)}</div>
    <div style="font-size:12px;color:#c7d6e6;margin-top:3px">
      {len(ranked)} new role{'s' if len(ranked) != 1 else ''} worth your time</div>
    {dash_button}
  </div>
  <div style="background:#fff;padding:20px;border-radius:0 0 10px 10px">
    {now_open_html}{urgent_html}
    {cards}
    {fail_html}
    <div style="font-size:11px;color:#8b98a5;margin-top:22px;border-top:1px solid #eceff1;
         padding-top:12px">
      {sheet_link}Scored 1-100 for fit and attainability &middot; {_esc(stats_line)}
    </div>
  </div>
</div></body></html>"""


def send_digest(html: str, settings: Settings) -> str:
    """Returns a human-readable delivery mode string for the run log."""
    to_addr = settings.cfg.get("digest_to") or settings.env("DIGEST_TO")
    subject = f"Job Bank digest — {date.today().isoformat()}"
    resend_key = settings.env("RESEND_API_KEY")
    if resend_key and to_addr:
        resp = httpx.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {resend_key}"},
                          json={"from": settings.env("DIGEST_FROM", "jobbank@resend.dev"),
                                "to": [to_addr], "subject": subject, "html": html},
                          timeout=30)
        resp.raise_for_status()
        return f"emailed via Resend to {to_addr}"
    smtp_host = settings.env("SMTP_HOST")
    if smtp_host and to_addr:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(html, "html")
        msg["Subject"], msg["To"] = subject, to_addr
        msg["From"] = settings.env("DIGEST_FROM", settings.env("SMTP_USER"))
        with smtplib.SMTP(smtp_host, int(settings.env("SMTP_PORT", "587"))) as s:
            s.starttls()
            if settings.env("SMTP_USER"):
                s.login(settings.env("SMTP_USER"), settings.env("SMTP_PASSWORD"))
            s.send_message(msg)
        return f"emailed via SMTP to {to_addr}"
    out = settings.out_dir / "digest.html"
    out.write_text(html, encoding="utf-8")
    return f"dry-run: written to {out}"
