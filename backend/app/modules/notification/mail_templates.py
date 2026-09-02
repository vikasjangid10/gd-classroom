"""Rendering of the two emails this product sends.

Both are built here rather than in the services that trigger them, so the wording and
the markup live together and neither invitation nor session code has to know HTML.

Email is not a browser. The rules followed below are the boring ones that actually
survive Gmail and Outlook: a table for layout, inline styles only, no web fonts, and a
visible copy of every link for the clients that strip the button.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from app.domain.ports import EmailMessage

_ACCENT = "#4f46e5"
_INK = "#111827"
_MUTED = "#6b7280"
_LINE = "#e5e7eb"


def _shell(heading: str, body_html: str, button_label: str, url: str) -> str:
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="background:#f4f4f5;padding:32px 12px;">
  <tr><td align="center">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="max-width:540px;background:#ffffff;border:1px solid {_LINE};border-radius:14px;\
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
      <tr><td style="padding:28px 32px 0;">
        <div style="font-size:11px;font-weight:700;letter-spacing:.12em;\
text-transform:uppercase;color:{_ACCENT};">AI GD Classroom</div>
        <h1 style="margin:12px 0 0;font-size:22px;line-height:1.3;color:{_INK};">{heading}</h1>
      </td></tr>
      <tr><td style="padding:16px 32px 0;font-size:15px;line-height:1.6;color:#374151;">
        {body_html}
      </td></tr>
      <tr><td style="padding:24px 32px 8px;">
        <a href="{escape(url, quote=True)}" \
style="display:inline-block;background:{_ACCENT};color:#ffffff;text-decoration:none;\
font-size:15px;font-weight:600;padding:13px 26px;border-radius:9px;">{button_label}</a>
      </td></tr>
      <tr><td style="padding:8px 32px 28px;font-size:12px;line-height:1.6;color:{_MUTED};">
        If the button does not work, paste this into your browser:<br>
        <span style="word-break:break-all;color:{_ACCENT};">{escape(url)}</span>
      </td></tr>
      <tr><td style="padding:16px 32px 24px;border-top:1px solid {_LINE};\
font-size:12px;line-height:1.6;color:{_MUTED};">
        The discussion is voice-only and lasts about 25 minutes. Nothing you say is kept
        after the session ends except the transcript your host chose to save.
      </td></tr>
    </table>
  </td></tr>
</table>"""


def _bullets(points: list[str]) -> str:
    if not points:
        return ""
    items = "".join(
        f'<li style="margin:4px 0;">{escape(point)}</li>' for point in points[:4]
    )
    return f'<ul style="margin:12px 0 0;padding-left:20px;color:#4b5563;">{items}</ul>'


def invitation_email(
    *,
    to: str,
    invitee_name: str,
    host_name: str,
    topic_title: str,
    topic_description: str,
    guiding_points: list[str],
    join_url: str,
    expires_at: datetime,
    reference: str | None = None,
) -> EmailMessage:
    expiry = expires_at.strftime("%d %b, %H:%M UTC")
    heading = f"{escape(host_name)} invited you to a group discussion"
    body = (
        f'<p style="margin:0;">Hi {escape(invitee_name)},</p>'
        f'<p style="margin:12px 0 0;">You have a seat in a four-person group discussion '
        f'on <strong style="color:{_INK};">{escape(topic_title)}</strong>, moderated by AI.</p>'
        f'<p style="margin:12px 0 0;color:#4b5563;">{escape(topic_description)}</p>'
        f"{_bullets(guiding_points)}"
        f'<p style="margin:16px 0 0;">Open the link on a device with a microphone. '
        f"The discussion begins once all four invitees have accepted.</p>"
        f'<p style="margin:12px 0 0;font-size:13px;color:{_MUTED};">'
        f"This invitation expires at {escape(expiry)}.</p>"
    )
    text = (
        f"Hi {invitee_name},\n\n"
        f"{host_name} invited you to a four-person, AI-moderated group discussion on "
        f'"{topic_title}".\n\n'
        f"{topic_description}\n\n"
        f"Accept or decline here:\n{join_url}\n\n"
        f"Open it on a device with a microphone — the discussion is voice-only and starts "
        f"once all four invitees accept. This invitation expires at {expiry}.\n\n"
        f"— AI GD Classroom"
    )
    return EmailMessage(
        to=to,
        subject=f"{host_name} invited you to discuss {topic_title}",
        html=_shell(heading, body, "Accept or decline", join_url),
        text=text,
        reference=reference,
    )


def session_ready_email(
    *,
    to: str,
    invitee_name: str,
    topic_title: str,
    room_url: str,
    reference: str | None = None,
) -> EmailMessage:
    heading = "Everyone accepted — your discussion is ready"
    body = (
        f'<p style="margin:0;">Hi {escape(invitee_name)},</p>'
        f'<p style="margin:12px 0 0;">All four participants have accepted. The discussion on '
        f'<strong style="color:{_INK};">{escape(topic_title)}</strong> is ready to begin.</p>'
        f'<p style="margin:12px 0 0;">Join from a quiet place, with headphones if you have '
        f"them — the moderator will introduce the topic and then call on each of you in turn.</p>"
    )
    text = (
        f"Hi {invitee_name},\n\n"
        f'All four participants accepted. Your discussion on "{topic_title}" is ready.\n\n'
        f"Join here:\n{room_url}\n\n"
        f"Use headphones if you have them. The AI moderator will introduce the topic and "
        f"then call on each participant in turn.\n\n"
        f"— AI GD Classroom"
    )
    return EmailMessage(
        to=to,
        subject=f"Your discussion on {topic_title} is starting",
        html=_shell(heading, body, "Join the discussion", room_url),
        text=text,
        reference=reference,
    )
