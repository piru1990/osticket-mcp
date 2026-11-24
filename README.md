# osticket-mcp

MCP server for an external **osTicket** portal (a software vendor's support desk) that has no API for the account in use, so it works through the web session.

- Read: `listar_tickets`, `ver_ticket`, `temas_de_ayuda`, `pagina`.
- Write (`dry_run=True` by default; confirm before sending): `crear_ticket`, `responder_ticket` (with attachments), and **`escalar_desde_odoo(ticket)`** which opens the vendor ticket from an Odoo one (subject `[Odoo #id]`), copies the attachments and leaves an internal note in Odoo with the vendor ticket number.
- Credentials live in the Windows Credential Manager (`_integra_env.py` in *office-automation-mcp-servers*).

## Español
Automatiza el portal de soporte de un proveedor (osTicket): consultar, crear, responder y escalar un ticket de Odoo con adjuntos y nota interna de seguimiento. Las escrituras son simulacro por defecto.

> **Note / Nota:** sanitized portfolio version of production work built for a corporate IT help desk. Company names, people, hosts, IDs and data were replaced with placeholders; secrets never lived in the code (they sit in the Windows Credential Manager). / Versión anonimizada de un trabajo real de mesa de ayuda corporativa: nombres, hosts, IDs y datos fueron reemplazados; los secretos nunca estuvieron en el código.
