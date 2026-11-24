# Security policy

Family Tasks stores credentials for task services (passwords, app passwords, OAuth tokens) in Home Assistant config entries.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. You should get a reply within a week.

Please treat these as vulnerabilities:
- credentials, tokens or credential-bearing URLs appearing in logs, diagnostics, events, entity attributes or the websocket API
- ways to read or change tasks without Home Assistant authorisation
- injection through task content into the card (XSS) or into iCalendar/XML requests

## Supported versions

Only the latest release receives fixes.
