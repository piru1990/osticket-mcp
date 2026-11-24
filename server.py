"""MCP: portal de soporte de ERP-PARTNER (osTicket, https://support.erp-partner.example.com). Soporte de SAP B1.

El portal no tiene API de cliente: se ingresa como en el navegador (login.php con __CSRFToken__,
luser y lpasswd) y se lee el HTML. La credencial viene del Administrador de credenciales de
Windows (_integra_env.py).

Lectura:
  listar_tickets(estado, texto)  tickets del usuario en el portal.
  ver_ticket(numero)             datos y conversación de un ticket.
  pagina(path)                   texto y formularios de tickets.php / open.php (diagnóstico).
No cierra sesión, no cierra ni reabre tickets.
"""
import base64
import json
import logging
import mimetypes
import re
import shutil
import sys
import tempfile
import threading
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _integra_env  # noqa: E402
import _odoo_env  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
mcp = FastMCP("integra-soporte")
RO = ToolAnnotations(readOnlyHint=True)

_PATH_OK = re.compile(r"^(tickets|open|view)\.php(\?[A-Za-z0-9_=&.%+-]*)?$")
_ESTADOS = {"open": "open", "abiertos": "open", "closed": "closed", "cerrados": "closed", "all": ""}


def _limpio(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


# ---------------------------------------------------------------- análisis del HTML
class _Pagina(HTMLParser):
    """Texto legible, tablas (filas con su primer enlace), formularios y mensajes de error."""
    _SALTAR = {"script", "style", "noscript"}
    _BLOQUE = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section", "article", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.lines: list[str] = []
        self.tablas: list[list[dict]] = []
        self.forms: list[dict] = []
        self.csrf = ""
        self._buf: list[str] = []
        self._skip = 0
        self._in_title = False
        self._fila: dict | None = None
        self._celda: list[str] | None = None
        self._select: dict | None = None
        self._opt: dict | None = None
        self._form: dict | None = None

    def _flush(self):
        t = _limpio("".join(self._buf))
        if t:
            self.lines.append(t)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self._SALTAR:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        if tag in self._BLOQUE:
            self._flush()
        if tag == "table":
            self.tablas.append([])
        elif tag == "tr" and self.tablas:
            self._fila = {"celdas": [], "href": ""}
        elif tag in ("td", "th") and self._fila is not None:
            self._celda = []
        elif tag == "a" and self._fila is not None and not self._fila["href"] and a.get("href"):
            self._fila["href"] = a["href"]
        elif tag == "form":
            self._form = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(), "campos": []}
            self.forms.append(self._form)
        elif tag == "input":
            n, ty = a.get("name", ""), (a.get("type") or "text").lower()
            if n == "__CSRFToken__":
                self.csrf = a.get("value", "")
            if self._form is not None and n:
                self._form["campos"].append({"name": n, "type": ty, "value": a.get("value", "") if ty in ("hidden", "submit") else ""})
        elif tag in ("textarea", "select") and self._form is not None and a.get("name"):
            self._select = {"name": a["name"], "type": tag, "opciones": []}
            self._form["campos"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._opt = {"value": a.get("value", ""), "texto": ""}
            self._select["opciones"].append(self._opt)

    def handle_endtag(self, tag):
        if tag in self._SALTAR and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("td", "th") and self._celda is not None and self._fila is not None:
            self._fila["celdas"].append(_limpio("".join(self._celda)))
            self._celda = None
        elif tag == "tr" and self._fila is not None:
            if self._fila["celdas"] and self.tablas:
                self.tablas[-1].append(self._fila)
            self._fila = None
        elif tag == "select":
            self._select = self._opt = None
        elif tag == "option":
            self._opt = None
        elif tag == "form":
            self._form = None
        if tag in self._BLOQUE:
            self._flush()

    def handle_data(self, d):
        if self._skip:
            return
        if self._in_title:
            self.title += d
            return
        if self._opt is not None:
            self._opt["texto"] += d
        if self._celda is not None:
            self._celda.append(d)
        self._buf.append(d)

    def close(self):
        super().close()
        self._flush()


def _analizar(html: str) -> _Pagina:
    p = _Pagina()
    p.feed(html)
    p.close()
    for f in p.forms:
        for c in f["campos"]:
            for o in c.get("opciones", []):
                o["texto"] = _limpio(o["texto"])
    p.title = _limpio(p.title)
    return p


# ---------------------------------------------------------------- sesión
class _Sesion:
    def __init__(self):
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._base = ""

    def _login(self) -> None:
        base, user, pwd = _integra_env.credentials()
        # sitio externo: sí pasa por el proxy corporativo (HTTPS_PROXY del registro del MCP)
        c = httpx.Client(follow_redirects=True, timeout=60, trust_env=True,
                         headers={"User-Agent": "Mozilla/5.0 (mcp-integra-soporte)"})
        r = c.get(f"{base}/login.php")
        csrf = _analizar(r.text).csrf
        r = c.post(f"{base}/login.php", data={"__CSRFToken__": csrf, "luser": user, "lpasswd": pwd})
        if "lpasswd" in r.text:
            c.close()
            raise RuntimeError("ERP-PARTNER rechazó el usuario o la contraseña guardados "
                               f"(credencial 'integra/portal'). Revise con: python {Path(_integra_env.__file__)} set")
        self._client, self._base = c, base

    def _necesita_login(self, r: httpx.Response) -> bool:
        return r.status_code in (401, 403, 422) or "lpasswd" in r.text

    def get(self, path: str) -> httpx.Response:
        with self._lock:
            for intento in (1, 2):
                if self._client is None:
                    self._login()
                r = self._client.get(urljoin(self._base + "/", path))
                if self._necesita_login(r) and intento == 1:
                    self._client.close()          # la sesión caducó: reingresar una vez
                    self._client = None
                    continue
                return r
        raise RuntimeError("No se pudo mantener la sesión de ERP-PARTNER.")

    def post(self, path: str, data: dict) -> httpx.Response:
        """POST sin reintento automático (un reintento podría duplicar el ticket): el token CSRF
        y la sesión se validan antes con un GET del formulario."""
        with self._lock:
            if self._client is None:
                self._login()
            return self._client.post(urljoin(self._base + "/", path), data=data)

    def post_files(self, path: str, data: dict, files: dict, headers: dict | None = None) -> httpx.Response:
        """POST multipart (subida de adjuntos). Igual que post: sin reintento."""
        with self._lock:
            if self._client is None:
                self._login()
            return self._client.post(urljoin(self._base + "/", path), data=data, files=files, headers=headers)


_S = _Sesion()


def _test() -> str:
    """Para `_integra_env.py status`: ingresa y cuenta los tickets visibles."""
    _S._login()
    r = _S.get("tickets.php?status=")
    filas = [f for t in _analizar(r.text).tablas for f in t if "id=" in f["href"]]
    return f"ingreso OK ({len(filas)} tickets visibles)"


def _fila_ticket(f: dict) -> dict:
    m = re.search(r"[?&]id=(\d+)", f["href"])
    return {"id": m.group(1) if m else "", "celdas": f["celdas"], "href": f["href"]}


# ---------------------------------------------------------------- herramientas
@mcp.tool(annotations=RO)
def listar_tickets(estado: str = "all", texto: str = "", limite: int = 50) -> dict:
    """Tickets de ERP-PARTNER visibles para el usuario. estado: 'open', 'closed' o 'all'.
    texto filtra por cualquier columna (sin distinguir mayúsculas). Cada fila trae `id` (el id
    interno del portal, que usa `ver_ticket`) y `celdas` (número, fecha, estado, asunto…, en el
    orden de la tabla del portal)."""
    est = _ESTADOS.get(estado.lower())
    if est is None:
        raise ValueError("estado debe ser 'open', 'closed' o 'all'.")
    r = _S.get(f"tickets.php?status={est}" if est else "tickets.php")
    p = _analizar(r.text)
    filas = [_fila_ticket(f) for t in p.tablas for f in t if "id=" in f["href"]]
    if texto:
        q = texto.lower()
        filas = [f for f in filas if q in " ".join(f["celdas"]).lower()]
    return {"total": len(filas), "mostrados": min(len(filas), limite), "tickets": filas[:limite]}


@mcp.tool(annotations=RO)
def ver_ticket(id: str, max_chars: int = 12000) -> dict:
    """Datos y conversación de un ticket de ERP-PARTNER. `id` es el id interno que devuelve
    `listar_tickets` (no el número visible, que puede ser distinto)."""
    if not re.fullmatch(r"\d{1,9}", id.strip()):
        raise ValueError("id debe ser numérico (el campo `id` de listar_tickets).")
    r = _S.get(f"tickets.php?id={id.strip()}")
    p = _analizar(r.text)
    texto = "\n".join(p.lines)
    adjuntos = [c["name"] for f in p.forms for c in f["campos"] if c.get("type") == "file"]
    return {"id": id, "status": r.status_code, "titulo": p.title, "texto": texto[:max_chars],
            "truncado": len(texto) > max_chars, "caracteres": len(texto), "adjuntos_campos": adjuntos,
            "archivos": _archivos_del_hilo(r.text)}


@mcp.tool(annotations=RO)
def pagina(path: str, max_chars: int = 8000) -> dict:
    """Diagnóstico: GET de tickets.php u open.php (con su query) y devuelve título, texto y
    formularios (campos, tipos, opciones). Sirve para ver los temas de ayuda y los campos de
    open.php antes de crear tickets. No envía formularios."""
    path = path.strip().replace("\\", "/")
    if not _PATH_OK.match(path):
        raise ValueError("path no válido: use tickets.php, open.php o view.php con su query.")
    r = _S.get(path)
    p = _analizar(r.text)
    texto = "\n".join(p.lines)
    return {"path": path, "status": r.status_code, "titulo": p.title,
            "formularios": [f for f in p.forms if f["campos"]][:5],
            "texto": texto[:max_chars], "truncado": len(texto) > max_chars}


# ---------------------------------------------------------------- adjuntos
# El portal (osTicket) sube con el "filedrop" de JavaScript: POST multipart a ajax.php/form/upload/attach
# con el campo `upload[]`; la respuesta trae el id del archivo y el formulario lo envía después en un
# campo oculto de nombre aleatorio por sesión (`name: 'xxxx[]'` en el script de la página).
# Leído del HTML el 2026-11-16: máx. 2 MB por archivo, 20 archivos, sin restricción de extensión.
# La subida real NO está probada contra el portal (solo se probó leer el HTML y los dry_run).
_ADJ_MB, _ADJ_N = 2, 20


def _config_adjuntos(html: str) -> dict:
    campo = re.search(r"name:\s*'([0-9a-f]+\[\])'", html)
    url = re.search(r"url:\s*'([^']*ajax\.php/form/upload/attach)'", html)
    if not (campo and url):
        raise RuntimeError("No encontré la configuración de adjuntos (filedrop) en la página del portal: ¿cambió?")
    mb = re.search(r"maxfilesize:\s*(\d+)", html)
    n = re.search(r"maxfiles:\s*(\d+)", html)
    return {"campo": campo.group(1), "url": url.group(1),
            "max_mb": int(mb.group(1)) if mb else _ADJ_MB, "max_n": int(n.group(1)) if n else _ADJ_N}


def _revisar_adjuntos(rutas: list[str], cfg: dict) -> list[dict]:
    """Valida archivos locales contra los límites del portal. No lee el contenido."""
    filas = []
    for r in rutas:
        p = Path(r)
        fila = {"ruta": str(p), "nombre": p.name, "bytes": 0, "ok": True, "motivo": ""}
        if not p.is_file():
            fila.update(ok=False, motivo="no existe")
        else:
            fila["bytes"] = p.stat().st_size
            if fila["bytes"] == 0:
                fila.update(ok=False, motivo="está vacío")
            elif fila["bytes"] > cfg["max_mb"] * 1024 * 1024:
                fila.update(ok=False, motivo=f"pesa más de {cfg['max_mb']} MB")
        filas.append(fila)
    if len(filas) > cfg["max_n"]:
        for f in filas[cfg["max_n"]:]:
            f.update(ok=False, motivo=f"pasa del máximo de {cfg['max_n']} archivos")
    return filas


def _id_de_subida(r: httpx.Response) -> str:
    """El id del archivo subido: acepta 123, "123", {"id": 123} o [123]."""
    if r.status_code >= 300:
        return ""
    try:
        v = r.json()
    except Exception:
        v = r.text.strip()
    for _ in range(3):
        if isinstance(v, list) and v:
            v = v[0]
        elif isinstance(v, dict):
            v = v.get("id") or v.get("file_id") or ""
        else:
            break
    s = str(v).strip().strip('"')
    return s if re.fullmatch(r"\d{1,12}", s) else ""


def _subir_adjuntos(rutas: list[str], cfg: dict, csrf: str) -> list[str]:
    """Sube cada archivo y devuelve sus ids. Si uno falla se detiene (los ya subidos quedan huérfanos
    en el portal, que los purga) y no se envía el ticket."""
    ids = []
    for r in rutas:
        p = Path(r)
        tipo = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        with p.open("rb") as fh:
            resp = _S.post_files(cfg["url"], {"__CSRFToken__": csrf}, {"upload[]": (p.name, fh, tipo)},
                                 headers={"X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf})
        fid = _id_de_subida(resp)
        if not fid:
            raise RuntimeError(f"El portal no aceptó «{p.name}»: HTTP {resp.status_code} {resp.text[:200]!r}. "
                               "No se envió nada más.")
        ids.append(fid)
    return ids


def _archivos_del_hilo(html: str) -> list[dict]:
    """Archivos que aparecen en la conversación de un ticket (nombre y enlace de descarga del portal)."""
    vistos, out = set(), []

    def agregar(nombre: str, url: str) -> None:
        k = re.search(r"key=([^&]+)", url)
        clave = k.group(1) if k else url
        if clave not in vistos and nombre:
            vistos.add(clave)
            out.append({"nombre": nombre, "url": url})

    for m in re.finditer(r'"download_url":"([^"]+)","filename":"([^"]+)"', html):
        agregar(unescape(m.group(2)), m.group(1).replace("\\/", "/"))
    for m in re.finditer(r'href="(/?file\.php\?[^"]+)"[^>]*>([^<]+)<', html):
        agregar(unescape(m.group(2)).strip(), unescape(m.group(1)))
    return out


def _html(texto: str) -> str:
    """Texto plano -> HTML simple para el editor del portal (párrafos y saltos de línea)."""
    from html import escape
    parrafos = [p for p in re.split(r"\n\s*\n", texto.strip()) if p.strip()]
    return "".join("<p>" + escape(p).replace("\n", "<br />") + "</p>" for p in parrafos)


def _form(html: str, accion: str) -> tuple[_Pagina, dict]:
    p = _analizar(html)
    for f in p.forms:
        if accion in f["action"]:
            return p, f
    raise RuntimeError(f"No encontré el formulario '{accion}' en el portal (¿cambió la página?).")


@mcp.tool()
def temas_de_ayuda() -> dict:
    """Temas de ayuda de open.php (valor `topicId` y nombre) y el campo de asunto de cada uno
    (su nombre cambia por sesión; crear_ticket lo resuelve solo)."""
    _, f = _form(_S.get("open.php").text, "open.php")
    t = next(c for c in f["campos"] if c["name"] == "topicId")
    return {"temas": [{"topicId": o["value"], "nombre": o["texto"]} for o in t["opciones"] if o["value"]]}


@mcp.tool()
def crear_ticket(tema: str, asunto: str, mensaje: str, adjuntos: list[str] | None = None,
                 dry_run: bool = True) -> dict:
    """Crea un ticket en ERP-PARTNER. tema = topicId (ver temas_de_ayuda; 12 = «Incidente SAP B1»).
    asunto = «Descripción corta» (máx. 50 caracteres). mensaje = texto plano (se convierte a HTML).
    adjuntos = rutas de archivos locales (máx. 2 MB c/u y 20; el portal no restringe extensión).
    dry_run=True (por defecto) solo arma el envío y lo muestra (valida los adjuntos pero NO los sube);
    con False sube los adjuntos y crea el ticket de verdad: hacerlo solo cuando el usuario lo haya
    confirmado en el chat."""
    if not re.fullmatch(r"\d{1,4}", tema.strip()):
        raise ValueError("tema debe ser el topicId numérico (ver temas_de_ayuda).")
    if not (asunto.strip() and mensaje.strip()):
        raise ValueError("asunto y mensaje son obligatorios.")
    if len(asunto.strip()) > 50:
        raise ValueError(f"asunto de {len(asunto.strip())} caracteres: el portal admite 50 como máximo.")
    html_open = _S.get(f"open.php?topicId={tema.strip()}").text
    p, f = _form(html_open, "open.php")
    t = next(c for c in f["campos"] if c["name"] == "topicId")
    if tema.strip() not in [o["value"] for o in t["opciones"]]:
        raise ValueError(f"topicId {tema} no existe (ver temas_de_ayuda).")
    libres = [c["name"] for c in f["campos"] if c.get("type") == "text"]
    if len(libres) != 1:
        raise RuntimeError(f"Se esperaba un solo campo de asunto y hay {libres}: revise con pagina('open.php?topicId={tema}').")
    datos = {"__CSRFToken__": p.csrf, "a": "open", "topicId": tema.strip(),
             libres[0]: asunto.strip(), "message": _html(mensaje)}
    resumen = {"tema": tema, "campo_asunto": libres[0], "asunto": asunto.strip(), "mensaje_html": datos["message"]}
    if adjuntos:
        cfg = _config_adjuntos(html_open)
        revision = _revisar_adjuntos(adjuntos, cfg)
        resumen["adjuntos"] = [{k: a[k] for k in ("nombre", "bytes", "ok", "motivo")} for a in revision]
        resumen["campo_adjuntos"] = cfg["campo"]
        malos = [a for a in revision if not a["ok"]]
        if malos and not dry_run:
            raise ValueError("Adjuntos no válidos: " + "; ".join(f"{a['nombre']} ({a['motivo']})" for a in malos))
    if dry_run:
        return {"dry_run": True, "se_enviaria": resumen}
    if adjuntos:
        datos[cfg["campo"]] = _subir_adjuntos(adjuntos, cfg, p.csrf)
    r = _S.post("open.php", datos)
    m = re.search(r"[?&]id=(\d+)", str(r.url))
    q = _analizar(r.text)
    if not m:
        errores = [l for l in q.lines if "error" in l.lower() or "requerid" in l.lower()][:5]
        return {"creado": False, "status": r.status_code, "url": str(r.url), "errores": errores, "titulo": q.title}
    num = re.search(r"#([A-Z]+\d+)", " ".join(q.lines))
    return {"creado": True, "id": m.group(1), "numero": num.group(1) if num else "", "url": str(r.url)}


@mcp.tool()
def responder_ticket(id: str, mensaje: str, adjuntos: list[str] | None = None, dry_run: bool = True) -> dict:
    """Responde en un ticket de ERP-PARTNER (id interno de listar_tickets). adjuntos = rutas locales
    (máx. 2 MB c/u y 20). dry_run=True (por defecto) solo muestra lo que enviaría (valida los
    adjuntos pero NO los sube); con False sube y publica la respuesta: solo con confirmación del
    usuario en el chat."""
    if not re.fullmatch(r"\d{1,9}", id.strip()):
        raise ValueError("id debe ser numérico.")
    if not mensaje.strip():
        raise ValueError("mensaje vacío.")
    html_tk = _S.get(f"tickets.php?id={id.strip()}").text
    p, f = _form(html_tk, "#reply")
    ocultos = {c["name"]: c["value"] for c in f["campos"] if c.get("type") == "hidden"}
    datos = {**ocultos, "__CSRFToken__": p.csrf, "message": _html(mensaje)}
    if adjuntos:
        cfg = _config_adjuntos(html_tk)
        revision = _revisar_adjuntos(adjuntos, cfg)
        malos = [a for a in revision if not a["ok"]]
        if malos and not dry_run:
            raise ValueError("Adjuntos no válidos: " + "; ".join(f"{a['nombre']} ({a['motivo']})" for a in malos))
    if dry_run:
        se = {"id": id, "campos": sorted(datos), "mensaje_html": datos["message"]}
        if adjuntos:
            se["adjuntos"] = [{k: a[k] for k in ("nombre", "bytes", "ok", "motivo")} for a in revision]
            se["campo_adjuntos"] = cfg["campo"]
        return {"dry_run": True, "se_enviaria": se}
    if adjuntos:
        datos[cfg["campo"]] = _subir_adjuntos(adjuntos, cfg, p.csrf)
    r = _S.post(f"tickets.php?id={id.strip()}", datos)
    q = _analizar(r.text)
    ok = r.status_code < 400 and _limpio(re.sub(r"<[^>]+>", " ", datos["message"]))[:40] in " ".join(q.lines)
    return {"publicado": ok, "status": r.status_code, "url": str(r.url)}


# ---------------------------------------------------------------- puente con Odoo
def _odoo(model: str, method: str, **params):
    """POST /json/2/<modelo>/<método> (misma API key que el MCP odoo-helpdesk). Reintenta solo los
    502/503/«No database is selected», que no llegan a la base."""
    url, key = _odoo_env.credentials()
    url = url.rstrip("/")
    if url.endswith("/odoo"):
        url = url[: -len("/odoo")]
    for intento in range(4):
        r = httpx.post(f"{url}/json/2/{model}/{method}", json=params, timeout=60, trust_env=True,
                       headers={"Authorization": f"bearer {key}", "User-Agent": "odoo-mcp"})
        if intento < 3 and (r.status_code in (502, 503) or (r.status_code == 404 and "No database is selected" in r.text)):
            time.sleep(5 * (intento + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Odoo {r.status_code} en {model}.{method}: {r.text[:500]}")
        return r.json()


def _texto_html(body) -> str:
    if not body:
        return ""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", str(body))
    t = re.sub(r"(?i)<li[^>]*>", "- ", t)
    t = unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def _id_odoo(ticket) -> int:
    s = str(ticket).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"[?&]res_id=(\d+)", s) or re.search(r"/(\d+)(?=[/?#]|$)", s)
    if not m:
        raise ValueError(f"No pude obtener el número de ticket de Odoo de: {ticket}")
    return int(m.group(1))


@mcp.tool()
def escalar_desde_odoo(ticket_odoo: int | str, tema: str = "12", asunto: str = "", mensaje: str = "",
                       incluir_adjuntos: bool = True, adjuntos_locales: list[str] | None = None,
                       dry_run: bool = True) -> dict:
    """Escala un ticket de Odoo (helpdesk.ticket) a ERP-PARTNER: crea el ticket en el portal con asunto
    «[Odoo #id] …», pasa los adjuntos del ticket (≤ 2 MB) y deja una nota INTERNA en Odoo con el
    número de ERP-Partner. ticket_odoo = número o URL. asunto/mensaje opcionales (por defecto salen del
    ticket de Odoo). incluir_adjuntos=False omite los adjuntos que están en Odoo; adjuntos_locales =
    rutas de archivos de esta PC que también se envían (≤ 2 MB c/u). Si ya hay un ticket «[Odoo #id]» en ERP-Partner no crea otro y lo devuelve.
    dry_run=True (por defecto) no sube ni crea nada y muestra el envío; con False crea el ticket
    en ERP-PARTNER y la nota en Odoo: solo con confirmación del usuario en el chat."""
    tid = _id_odoo(ticket_odoo)
    marca = f"[Odoo #{tid}]"
    filas = _odoo("helpdesk.ticket", "read", ids=[tid], fields=["name", "description", "partner_id", "user_id"])
    if not filas:
        raise ValueError(f"No existe el ticket de Odoo {tid}.")
    t = filas[0]
    previos = listar_tickets("all", marca, 5)["tickets"]
    if previos:
        return {"ya_escalado": True, "tickets_integra": [{"id": x["id"], "celdas": x["celdas"]} for x in previos],
                "aviso": "No se creó otro: ya hay un ticket en ERP-PARTNER con esa marca."}

    asunto_f = asunto.strip() or f"{marca} {t['name']}"
    if marca not in asunto_f:
        asunto_f = f"{marca} {asunto_f}"
    asunto_f = asunto_f[:50].rstrip()
    solicitante = t["partner_id"][1] if t.get("partner_id") else "(sin cliente)"
    mensaje_f = mensaje.strip() or (
        f"Escalamiento desde la Mesa de Ayuda de Acme (ticket interno {marca}).\n"
        f"Solicitante: {solicitante}\nAsunto: {t['name']}\n\n{_texto_html(t.get('description')) or '(sin descripción)'}")

    cfg = _config_adjuntos(_S.get(f"open.php?topicId={tema.strip()}").text)
    candidatos, omitidos = [], []
    if incluir_adjuntos:
        for a in _odoo("ir.attachment", "search_read",
                       domain=[["res_model", "=", "helpdesk.ticket"], ["res_id", "=", tid], ["type", "=", "binary"]],
                       fields=["name", "file_size"], order="id"):
            if not a["file_size"] or a["file_size"] > cfg["max_mb"] * 1024 * 1024:
                omitidos.append({"nombre": a["name"], "bytes": a["file_size"], "motivo": f"vacío o > {cfg['max_mb']} MB"})
            elif len(candidatos) >= cfg["max_n"]:
                omitidos.append({"nombre": a["name"], "bytes": a["file_size"], "motivo": f"pasa de {cfg['max_n']} archivos"})
            else:
                candidatos.append(a)
    locales = _revisar_adjuntos(adjuntos_locales or [], cfg)
    if len(locales) + len(candidatos) > cfg["max_n"]:
        raise ValueError(f"Son más de {cfg['max_n']} archivos entre los de Odoo y los locales.")
    resumen = {"ticket_odoo": tid, "tema": tema, "asunto": asunto_f, "mensaje": mensaje_f,
               "adjuntos": ([{"nombre": a["nombre"], "bytes": a["bytes"], "ok": a["ok"], "motivo": a["motivo"]} for a in locales]
                            + [{"nombre": a["name"], "bytes": a["file_size"]} for a in candidatos]),
               "adjuntos_omitidos": omitidos}
    if not dry_run and any(not a["ok"] for a in locales):
        raise ValueError("Adjuntos locales no válidos: " + "; ".join(f"{a['nombre']} ({a['motivo']})" for a in locales if not a["ok"]))
    if dry_run:
        return {"dry_run": True, "se_enviaria": resumen, "nota_odoo": "Nota interna con el número de ERP-PARTNER y su enlace."}

    carpeta = Path(tempfile.mkdtemp(prefix="integra-adj-"))
    try:
        rutas = [a["ruta"] for a in locales]
        for i, a in enumerate(candidatos):
            datos = _odoo("ir.attachment", "read", ids=[a["id"]], fields=["datas"])[0]["datas"]
            nombre = re.sub(r'[\\/:*?"<>|]', "_", a["name"]) or f"adjunto{i}"
            (carpeta / str(i)).mkdir()          # una carpeta por archivo: el portal ve el nombre original
            ruta = carpeta / str(i) / nombre
            ruta.write_bytes(base64.b64decode(datos))
            rutas.append(str(ruta))
        res = crear_ticket(tema, asunto_f, mensaje_f, rutas or None, dry_run=False)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)
    if not res.get("creado"):
        return {"creado": False, "integra": res, "aviso": "No se creó el ticket en ERP-PARTNER; no se escribió nada en Odoo."}
    num, url = res.get("numero") or f"id {res.get('id')}", res.get("url", "")
    cuerpo = (f"<p><b>Escalado a ERP-PARTNER</b>: ticket <b>{num}</b> (id interno {res.get('id')}).</p>"
              f"<p>Asunto: {asunto_f}<br/>Adjuntos enviados: {len(rutas)}<br/>Enlace: {url}</p>"
              "<p>Pendiente: seguimiento de la respuesta de ERP-PARTNER.</p>")
    try:
        _odoo("helpdesk.ticket", "message_post", ids=[tid], body=cuerpo, body_is_html=True,
              message_type="comment", subtype_xmlid="mail.mt_note")
        nota_ok, nota_err = True, ""
    except Exception as e:  # el ticket de ERP-PARTNER ya existe: no reintentar la creación
        nota_ok, nota_err = False, str(e)
    return {"creado": True, "integra": res, "nota_odoo": nota_ok, "nota_odoo_error": nota_err,
            "adjuntos_enviados": len(rutas)}


if __name__ == "__main__":
    mcp.run()
