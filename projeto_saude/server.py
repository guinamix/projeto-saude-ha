#!/usr/bin/env python3
"""Projeto Saúde — add-on do Home Assistant.

Dois servidores:
  * 8099 (ingress): serve o app e a API, identificando o usuário do Home Assistant
    pelos cabeçalhos X-Remote-User-* que o ingress envia.
  * 8765 (atalhos): recebe o texto enviado pelos atalhos do iPhone em
    POST /i/<token>, interpreta e grava nos registros do dono do token.
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

DATA_DIR = os.environ.get("DATA_DIR", "/data")
WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
DB_PATH = os.path.join(DATA_DIR, "saude.db")
INGRESS_IP = "172.30.32.2"
LOCK = threading.Lock()

os.makedirs(DATA_DIR, exist_ok=True)
CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
CONN.execute("PRAGMA journal_mode=WAL")
CONN.execute("""CREATE TABLE IF NOT EXISTS docs(
  usr TEXT NOT NULL, path TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY(usr, path))""")
CONN.execute("""CREATE TABLE IF NOT EXISTS users(
  usr TEXT PRIMARY KEY, name TEXT, token TEXT UNIQUE NOT NULL, rev INTEGER NOT NULL DEFAULT 0)""")
CONN.commit()


# ---------------------------------------------------------------- dados
def get_user(usr, name=None):
    with LOCK:
        row = CONN.execute("SELECT usr, name, token, rev FROM users WHERE usr=?", (usr,)).fetchone()
        if not row:
            tok = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
            CONN.execute("INSERT INTO users(usr, name, token, rev) VALUES(?,?,?,0)", (usr, name or "", tok))
            CONN.commit()
            row = (usr, name or "", tok, 0)
        elif name and name != row[1]:
            CONN.execute("UPDATE users SET name=? WHERE usr=?", (name, usr))
            CONN.commit()
    return {"id": row[0], "name": name or row[1], "token": row[2], "rev": row[3]}


def user_by_token(tok):
    with LOCK:
        row = CONN.execute("SELECT usr FROM users WHERE token=?", (tok,)).fetchone()
    return row[0] if row else None


def bump(usr):
    CONN.execute("UPDATE users SET rev=rev+1 WHERE usr=?", (usr,))


def rev_of(usr):
    with LOCK:
        row = CONN.execute("SELECT rev FROM users WHERE usr=?", (usr,)).fetchone()
    return row[0] if row else 0


def all_docs(usr):
    with LOCK:
        rows = CONN.execute("SELECT path, data FROM docs WHERE usr=?", (usr,)).fetchall()
    return {p: json.loads(d) for p, d in rows}


def get_doc(usr, path):
    row = CONN.execute("SELECT data FROM docs WHERE usr=? AND path=?", (usr, path)).fetchone()
    return json.loads(row[0]) if row else None


def put_doc(usr, path, data):
    CONN.execute("INSERT OR REPLACE INTO docs(usr, path, data, updated) VALUES(?,?,?,?)",
                 (usr, path, json.dumps(data, ensure_ascii=False), time.time()))


def write(usr, path, op, data=None):
    """op: set | update | delete. update num documento inexistente -> KeyError."""
    if not re.fullmatch(r"[A-Za-z0-9_\-.~:@+]{1,200}/[A-Za-z0-9_\-.~:@+]{1,200}", path or ""):
        raise ValueError("caminho inválido")
    with LOCK:
        if op == "delete":
            CONN.execute("DELETE FROM docs WHERE usr=? AND path=?", (usr, path))
        elif op == "set":
            put_doc(usr, path, data or {})
        elif op == "update":
            cur = get_doc(usr, path)
            if cur is None:
                raise KeyError(path)
            cur.update(data or {})
            put_doc(usr, path, cur)
        else:
            raise ValueError("op inválida")
        bump(usr)
        CONN.commit()


def merge(usr, path, data, novo=None):
    """Igual ao gravar() do app: mescla, ou cria com os campos padrão."""
    cur = get_doc(usr, path)
    if cur is None:
        cur = dict(novo or {})
    cur.update(data)
    put_doc(usr, path, cur)


# ------------------------------------------------- leitura dos atalhos
def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("p", ".").replace("P", ".").replace(",", "."))
    except ValueError:
        return None


def _iso(y, m, d):
    return f"{y}-{m}-{d}"


def parse_pesagem(h):
    """#pw-AAAAMMDD-kg[-gordura][-magra][-z-AAAAMMDD-tot-prof-rem-acord]"""
    sono = None
    zi = re.search(r"-z-", h, re.I)
    if zi:
        z = re.match(r"(\d{4})(\d{2})(\d{2})-([\dp.,]+)-([\dp.,]*)-([\dp.,]*)-([\dp.,]*)", h[zi.end():], re.I)
        if z:
            sec = lambda v: _num(v) or 0.0
            tot, prof, rem, acord = (sec(z.group(i)) / 60 for i in (4, 5, 6, 7))
            if tot > 0:
                sono = {"data": _iso(z.group(1), z.group(2), z.group(3)), "sonoTotal": round(tot),
                        "sono": round(tot - acord), "sonoProf": round(prof), "sonoRem": round(rem),
                        "sonoAcord": round(acord), "sonoEss": max(0, round(tot - prof - rem - acord))}
        h = h[:zi.start()]
    m = re.match(r"#pw-(\d{4})(\d{2})(\d{2})-([\dp.,]+)(?:-([\dp.,]*))?(?:-([\dp.,]*))?", h, re.I)
    peso = None
    if m:
        kg = _num(m.group(4))
        if kg and 30 <= kg <= 300:
            peso = {"data": _iso(m.group(1), m.group(2), m.group(3)), "kg": round(kg, 1)}
            g = _num(m.group(5))
            if g is not None:
                peso["gordura"] = round(g * 100, 1) if g <= 1 else round(g, 1)
            mg = _num(m.group(6))
            if mg is not None:
                peso["magra"] = round(mg, 1)
    return peso, sono


def parse_caminhadas(h):
    """#pm-AAAAMMDD-passosx..-metrosx..-minx..[-w-...][-e-...] (mais antigo -> hoje)"""
    seq = None
    ei = re.search(r"-e-", h, re.I)
    if ei:
        por_dia = {}
        for it in re.split(r"x", h[ei.end():], flags=re.I):
            mm = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(?:v(\d+))?", it.strip(), re.I)
            if not mm:
                continue
            d = _iso(mm.group(1), mm.group(2), mm.group(3))
            ini = int(mm.group(4)) * 60 + int(mm.group(5))
            dur = max(1, int(mm.group(6))) if mm.group(6) else 1
            por_dia.setdefault(d, []).append((ini, ini + dur))
        seq = {}
        for d, iv in por_dia.items():
            iv.sort()
            best, cs, ce = 0, None, None
            for a, b in iv:
                if cs is None or a > ce + 1:
                    cs, ce = a, b
                else:
                    ce = max(ce, b)
                best = max(best, ce - cs)
            seq[d] = best
        h = h[:ei.start()]
    treinos = []
    wi = re.search(r"-w-", h, re.I)
    if wi:
        for it in re.split(r"x", h[wi.end():], flags=re.I):
            mm = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})m(\d+)(?:[.,]\d+)?(?:k(\d+))?", it.strip(), re.I)
            if mm:
                treinos.append({"id": "wk-" + "".join(mm.group(i) for i in range(1, 6)),
                                "data": _iso(mm.group(1), mm.group(2), mm.group(3)),
                                "hora": mm.group(4) + ":" + mm.group(5), "minutos": int(mm.group(6)),
                                "metros": int(mm.group(7)) if mm.group(7) else None})
        h = h[:wi.start()]
    md = re.match(r"#p([dm])-(\d{4})(\d{2})(\d{2})-([^-]*)(?:-([^-]*))?(?:-([^-]*))?(?:-([^-]*))?", h, re.I)
    if not md:
        return None, treinos, seq
    em_metros = md.group(1).lower() == "m"
    hoje = date(int(md.group(2)), int(md.group(3)), int(md.group(4)))
    lista = lambda t: [x.strip() for x in re.split(r"[_x]", t or "", flags=re.I) if x.strip()]

    def inteiro(t):
        n = re.sub(r"[^\d]", "", t)
        return int(n) if n else None

    def km_de(t):
        if em_metros:
            n = inteiro(t)
            return None if n is None else round(n / 1000, 2)
        v = _num(re.sub(r"[^\d.,p]", "", t))
        return None if v is None else round(v, 2)

    P = [inteiro(t) for t in lista(md.group(5))]
    K = [km_de(t) for t in lista(md.group(6))]
    M = [inteiro(t) for t in lista(md.group(7))]
    Z = [inteiro(t) for t in lista(md.group(8))]
    n = max(len(P), len(K), len(M), len(Z))
    dias = []
    for i in range(n):
        r = {"data": (hoje - timedelta(days=i)).isoformat()}
        for nome, arr in (("passos", P), ("km", K), ("min", M), ("sono", Z)):
            if i < len(arr) and arr[len(arr) - 1 - i] is not None:
                r[nome] = arr[len(arr) - 1 - i]
        dias.append(r)
    return dias, treinos, seq


def ingest(usr, texto):
    """Interpreta o texto de um atalho e grava. Devolve um resumo."""
    texto = unquote(texto or "")
    m = re.search(r"#p[a-z]-[^\s\"']*", texto, re.I)
    if not m:
        return {"ok": False, "erro": "Não encontrei os dados no texto recebido."}
    h = m.group(0)
    resumo = {"ok": True}
    agora = int(time.time() * 1000)
    with LOCK:
        plano = get_doc(usr, "config/plano") or {}
        inicio = plano.get("projetoInicio") or ""
        if re.match(r"#pw-", h, re.I):
            peso, sono = parse_pesagem(h)
            if sono and not (inicio and sono["data"] < inicio):
                merge(usr, "registros/dia-" + sono["data"], sono, {"tipo": "atividade", "criadoEm": agora})
                resumo["sono"] = sono
            if peso and not (inicio and peso["data"] < inicio):
                merge(usr, "registros/peso-" + peso["data"],
                      dict(peso, tipo="peso", origem="balanca", criadoEm=agora))
                resumo["peso"] = peso
            if resumo.get("sono") or resumo.get("peso"):
                put_doc(usr, "config/syncPeso", {"em": agora})
        else:
            dias, treinos, seq = parse_caminhadas(h)
            for w in treinos:
                d = {"tipo": "treino", "origem": "watch", "modalidade": "Caminhada", "data": w["data"],
                     "hora": w["hora"], "minutos": w["minutos"], "criadoEm": agora}
                if w["metros"]:
                    d["km"] = round(w["metros"] / 1000, 2)
                put_doc(usr, "registros/" + w["id"], d)
            gravados = 0
            for r in dias or []:
                if inicio and r["data"] < inicio:
                    continue
                if not any(k in r for k in ("passos", "km", "min", "sono")):
                    continue
                if seq is not None:
                    r["seq"] = seq.get(r["data"], 0)
                merge(usr, "registros/dia-" + r["data"], dict(r, tipo="atividade", criadoEm=agora))
                gravados += 1
            if dias:
                put_doc(usr, "config/sync", {"em": agora, "dias": len(dias)})
            resumo["dias"] = gravados
        bump(usr)
        CONN.commit()
    return resumo


# --------------------------------------------------------- servidores
class Base(BaseHTTPRequestHandler):
    server_version = "ProjetoSaude/1.0"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/rev" in msg or "/api/state" in msg:
            return
        print("%s [%s] %s" % (time.strftime("%d/%m %H:%M:%S"), self.address_string(), msg))

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limite=8 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limite:
            raise ValueError("corpo grande demais")
        return self.rfile.read(n) if n else b""


class Ingress(Base):
    def usuario(self):
        uid = self.headers.get("X-Remote-User-Id")
        nome = self.headers.get("X-Remote-User-Display-Name") or self.headers.get("X-Remote-User-Name") or ""
        if not uid:
            if self.client_address[0] != INGRESS_IP and os.environ.get("DEV") != "1":
                return None
            uid = "local"
        return get_user(uid, nome)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            with open(os.path.join(WWW, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p.startswith("/static/"):
            nome = os.path.basename(p)
            arq = os.path.join(WWW, nome)
            if os.path.isfile(arq):
                tipo = "image/png" if nome.endswith(".png") else "application/octet-stream"
                with open(arq, "rb") as f:
                    return self.send(200, f.read(), tipo)
            return self.send(404, {"erro": "não encontrado"})
        u = self.usuario()
        if not u:
            return self.send(403, {"erro": "acesso só pelo Home Assistant"})
        if p == "/api/me":
            return self.send(200, {"id": u["id"], "name": u["name"], "token": u["token"]})
        if p == "/api/rev":
            return self.send(200, {"rev": rev_of(u["id"])})
        if p == "/api/state":
            return self.send(200, {"rev": rev_of(u["id"]), "docs": all_docs(u["id"])})
        return self.send(404, {"erro": "não encontrado"})

    def do_POST(self):
        p = urlparse(self.path).path
        u = self.usuario()
        if not u:
            return self.send(403, {"erro": "acesso só pelo Home Assistant"})
        try:
            dados = json.loads(self.body() or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"erro": "JSON inválido"})
        try:
            if p == "/api/doc":
                write(u["id"], dados.get("path"), dados.get("op"), dados.get("data"))
                return self.send(200, {"ok": True, "rev": rev_of(u["id"])})
            if p == "/api/import":
                docs = dados.get("docs") or {}
                with LOCK:
                    for path, d in docs.items():
                        if re.fullmatch(r"(registros|config)/[A-Za-z0-9_\-.~:@+]{1,200}", path) and isinstance(d, dict):
                            put_doc(u["id"], path, d)
                    bump(u["id"])
                    CONN.commit()
                return self.send(200, {"ok": True, "importados": len(docs), "rev": rev_of(u["id"])})
            if p == "/api/token":
                novo = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
                with LOCK:
                    CONN.execute("UPDATE users SET token=? WHERE usr=?", (novo, u["id"]))
                    CONN.commit()
                return self.send(200, {"token": novo})
            if p == "/api/testar":
                return self.send(200, ingest(u["id"], dados.get("texto", "")))
        except KeyError:
            return self.send(404, {"erro": "não encontrado"})
        except ValueError as e:
            return self.send(400, {"erro": str(e)})
        return self.send(404, {"erro": "não encontrado"})


class Atalhos(Base):
    def _ingest(self, tok, texto):
        usr = user_by_token(tok)
        if not usr:
            return self.send(404, {"ok": False, "erro": "Link inválido. Copie o link de novo no app."})
        r = ingest(usr, texto)
        return self.send(200 if r.get("ok") else 400, r)

    def do_POST(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/i/([A-Za-z0-9]+)", u.path)
        if not m:
            return self.send(404, {"erro": "não encontrado"})
        try:
            raw = self.body(256 * 1024).decode("utf-8", "replace")
        except ValueError as e:
            return self.send(400, {"erro": str(e)})
        return self._ingest(m.group(1), raw)

    def do_GET(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/i/([A-Za-z0-9]+)", u.path)
        if not m:
            return self.send(200, "Projeto Saúde: recebedor dos atalhos está no ar.", "text/plain; charset=utf-8")
        q = parse_qs(u.query).get("t", [""])[0]
        if not q:
            return self.send(200, {"ok": True, "msg": "Link válido." if user_by_token(m.group(1)) else "Link inválido."})
        return self._ingest(m.group(1), q)


def main():
    a = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("INGRESS_PORT", 8099))), Ingress)
    b = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("INGEST_PORT", 8765))), Atalhos)
    threading.Thread(target=b.serve_forever, daemon=True).start()
    print("Projeto Saúde no ar: app na porta %d, atalhos na porta %d" % (a.server_port, b.server_port))
    a.serve_forever()


if __name__ == "__main__":
    main()
