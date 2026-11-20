#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dashboard_firms.py - Dashboard cartografico local dos focos de calor (MySQL -> navegador).

Como usar
  python dashboard_firms.py
      Na primeira vez prepara as tabelas de resumo (mostra o progresso) e em seguida
      abre o navegador em http://127.0.0.1:8000. Nas proximas vezes abre direto; se
      voce carregou dados novos na firms_tratado, so as linhas novas sao processadas.

  python dashboard_firms.py --preparar    so prepara/atualiza os resumos e sai
  python dashboard_firms.py --refazer     apaga os resumos e refaz tudo do zero
  python dashboard_firms.py --apagar      remove do banco as tabelas firms_dash_*

Conexao (padrao): MySQL em localhost:3306, usuario root, sem senha, banco area_queimada.
  Para mudar: --host, --port, --user, --database; senha pela variavel MYSQL_PASSWORD.

Arquivos necessarios na mesma pasta:
  dashboard_firms.py   este programa
  dashboard.html       a pagina
  dashboard_geo.json   base do mapa (paises, estados, cidades, rios) - Natural Earth
Dependencia: pip install pymysql

O que o preparo faz
  A firms_tratado tem milhoes de linhas; agregar tudo a cada clique seria lento.
  O preparo le a tabela UMA vez, em blocos pela chave primaria, e grava tabelas de
  apoio com o prefixo firms_dash_ (a firms_tratado nunca e alterada):
    firms_dash_ponto     um registro compacto por foco, ordenado por regiao e data
                         (e a base de tudo que depende de filtro ou de zoom)
    firms_dash_cel_tot   celulas de 0,5 grau x combinacao, periodo todo   (mapa do mundo)
    firms_dash_cel_mes   celulas de 0,5 grau por mes                      (mapa do mundo)
    firms_dash_fino      celulas de 0,1 grau, periodo todo                (zoom em paises)
    firms_dash_serie     focos por dia e combinacao                       (serie temporal)
    firms_dash_dia       focos por dia e pais
    firms_dash_pais_tot  focos por pais e combinacao, periodo todo
    firms_dash_combo     combinacoes satelite/sensor/confianca/periodo/tipo/classe de FRP
    firms_dash_pais      paises (codigo ISO numerico, nome, continente)
    firms_dash_regiao    estados/provincias
    firms_dash_geo       celula de 0,1 grau -> pais e estado
    firms_dash_meta      controle (ultima linha processada etc.)
  Cada foco e atribuido ao pais/estado pela celula de 0,1 grau (cerca de 11 km) em que
  cai; focos no mar a mais de ~30 km da costa ficam "sem pais".
  As datas sao sempre UTC (coluna acq_datetime_utc).
"""
import argparse
import base64
import gzip
import hmac
import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
import webbrowser
import zlib
from array import array
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_HTML = os.path.join(PASTA, "dashboard.html")
ARQ_GEO = os.path.join(PASTA, "dashboard_geo.json")

VERSAO = "3"                 # formato das tabelas de resumo; mudar aqui forca refazer
TAB = "firms_tratado"
PRE = "firms_dash_"
TABELAS = ["meta", "combo", "pais", "regiao", "geo", "serie", "dia", "pais_tot",
           "cel_mes", "cel_tot", "fino", "ponto"]
COLUNAS = ["latitude", "longitude", "acq_datetime_utc", "satellite", "instrument",
           "confidence_nivel", "frp_mw", "classe_frp", "periodo", "tipo_cod"]

LIMITE_PONTOS = 20000        # acima disso o mapa mostra grade fina em vez de pontos
MAX_BLOCOS = 9000            # blocos de 1 grau por consulta de detalhe
TEMPO_MAX_MS = 60000         # consultas mais lentas que isso sao interrompidas
CACHE_BYTES = 192 * 1024 * 1024   # respostas ja calculadas ficam na memoria ate este limite
PARALELO = max(2, min(8, os.cpu_count() or 4))   # consultas simultaneas ao varrer a tabela de focos
DIVISOR = {10: 10000, 50: 2000, 100: 1000, 250: 400, 500: 200}   # celulas/grau -> divisor de (graus * 1e5)
EPOCA = date(1970, 1, 1)

ORD_CONF = ["Baixa", "Nominal", "Alta"]
ORD_PER = ["Dia", "Noite"]
ORD_SAT = ["Terra", "T", "Aqua", "A", "N", "SNPP", "N20", "NOAA-20", "J1", "N21", "NOAA-21", "J2"]
SEM_CLASSE = "~"             # como a classe vazia (foco sem FRP) aparece na URL


class Erro(Exception):
    """Erro com mensagem pronta para o usuario."""
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def fmt(n):
    return "{:,}".format(int(n)).replace(",", ".")


def dur(seg):
    seg = int(seg)
    if seg < 60:
        return "%ds" % seg
    if seg < 3600:
        return "%dmin%02ds" % (seg // 60, seg % 60)
    return "%dh%02dmin" % (seg // 3600, seg % 3600 // 60)


# ----------------------------------------------------------------------
# Base cartografica (dashboard_geo.json)
# ----------------------------------------------------------------------
class Geo:
    def __init__(self, caminho):
        try:
            with open(caminho, "rb") as f:
                bruto = f.read()
        except FileNotFoundError:
            raise Erro("Arquivo dashboard_geo.json nao encontrado ao lado de dashboard_firms.py", 500)
        self.assinatura = hashlib.md5(bruto).hexdigest()
        g = json.loads(bruto.decode("utf-8"))
        self.paises = {p["c"]: p for p in g["paises"]}
        self.continentes = g["continentes"]
        self.regioes = g["regioes"]              # indice + 1 = id; [pais, nome, lon, lat, zoom, w, s, e, n]
        gr = g["grade"]
        self.res, self.w, self.h = gr["res"], gr["w"], gr["h"]
        ras = array("H")
        ras.frombytes(zlib.decompress(base64.b64decode(gr["dados"])))
        if sys.byteorder == "big":
            ras.byteswap()
        if len(ras) != self.w * self.h:
            raise Erro("dashboard_geo.json corrompido (grade com tamanho errado)", 500)
        self.ras = ras
        # codigos "brutos": um pais pode ter mais de um (Russia europeia 643, asiatica 1643)
        brutos = set(self.paises) | {r[0] for r in self.regioes if r[0]}
        self.brutos = sorted(brutos)
        self.partes = {}
        for b in self.brutos:
            self.partes.setdefault(self.base(b), []).append(b)

    @staticmethod
    def base(p):
        return p - 1000 if p >= 1000 else p

    def cont_de(self, p):
        if p >= 1000:
            return "ASI"
        e = self.paises.get(p)
        return e["ct"] if e else None

    def codigos_pais(self, exib):
        return self.partes.get(exib, [])

    def codigos_cont(self, ct):
        return [b for b in self.brutos if self.cont_de(b) == ct]

    def reg_de(self, lat10, lon10):
        r, c = lat10 + 900, lon10 + 1800
        if 0 <= r < self.h and 0 <= c < self.w:
            return self.ras[r * self.w + c]
        return 0

    def pais_de_reg(self, reg):
        return self.regioes[reg - 1][0] if reg else 0


# ----------------------------------------------------------------------
# Banco
# ----------------------------------------------------------------------
def conectar(cfg):
    try:
        import pymysql
    except ImportError:
        raise Erro("Driver nao encontrado. Instale com: pip install pymysql", 500)
    try:
        return pymysql.connect(
            host=cfg.host, port=cfg.port, user=cfg.user,
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=cfg.database, charset="utf8mb4", autocommit=True,
        )
    except pymysql.err.OperationalError as e:
        cod = e.args[0] if e.args else 0
        if cod == 1049:
            msg = "O banco '%s' nao existe neste MySQL. Confira o nome com --database." % cfg.database
        elif cod == 1045:
            msg = ("O MySQL recusou o usuario '%s'. Se ele tem senha, defina a variavel MYSQL_PASSWORD "
                   "antes de rodar." % cfg.user)
        elif cod in (2002, 2003, 2005):
            msg = ("Nao consegui conectar ao MySQL em %s:%s. O servico do MySQL esta ligado?"
                   % (cfg.host, cfg.port))
        else:
            msg = "Erro ao conectar ao MySQL: %s" % (e,)
        raise Erro(msg, 503)


def inspeciona(cur):
    """Confere se a firms_tratado tem as colunas esperadas; devolve (coluna da chave, tipo dela)."""
    try:
        cur.execute("SHOW COLUMNS FROM " + TAB)
    except Exception as e:
        if "1146" in str(e) or "doesn't exist" in str(e):
            raise Erro("A tabela %s nao existe neste banco. Rode a carga primeiro." % TAB, 503)
        raise
    cols = cur.fetchall()
    nomes = {c[0].lower() for c in cols}
    faltam = [c for c in COLUNAS if c not in nomes]
    if faltam:
        raise Erro("A tabela %s nao tem a(s) coluna(s): %s" % (TAB, ", ".join(faltam)), 503)
    pk = [c for c in cols if c[3] == "PRI"]
    tipo = str(pk[0][1]).lower() if pk else ""
    if len(pk) != 1 or "int" not in tipo:
        raise Erro("A tabela %s precisa de uma chave primaria inteira (coluna id)." % TAB, 503)
    return pk[0][0], ("BIGINT UNSIGNED" if "bigint" in tipo else "INT UNSIGNED")


def ddl(tipo_id):
    m = "qtd INT UNSIGNED NOT NULL, frp_soma DOUBLE NOT NULL, frp_max FLOAT NOT NULL"
    txt = " DEFAULT CHARSET=utf8mb4"
    return OrderedDict([
        ("meta", ("chave VARCHAR(40) NOT NULL, valor MEDIUMTEXT, PRIMARY KEY (chave)", txt)),
        ("combo", ("combo SMALLINT UNSIGNED NOT NULL, satellite VARCHAR(40) NOT NULL, "
                   "instrument VARCHAR(40) NOT NULL, confidence_nivel VARCHAR(40) NOT NULL, "
                   "periodo VARCHAR(40) NOT NULL, tipo_cod SMALLINT NOT NULL, classe_frp VARCHAR(40) NOT NULL, "
                   "PRIMARY KEY (combo), "
                   "UNIQUE KEY uq (satellite, instrument, confidence_nivel, periodo, tipo_cod, classe_frp)",
                   " DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin")),
        ("pais", ("pais SMALLINT UNSIGNED NOT NULL, pais_base SMALLINT UNSIGNED NOT NULL, iso2 VARCHAR(2), "
                  "nome VARCHAR(100) NOT NULL, continente VARCHAR(60) NOT NULL, PRIMARY KEY (pais)", txt)),
        ("regiao", ("reg SMALLINT UNSIGNED NOT NULL, pais SMALLINT UNSIGNED NOT NULL, "
                    "nome VARCHAR(160) NOT NULL, PRIMARY KEY (reg), KEY k_pais (pais)", txt)),
        ("geo", ("lat10 SMALLINT NOT NULL, lon10 SMALLINT NOT NULL, pais SMALLINT UNSIGNED NOT NULL, "
                 "reg SMALLINT UNSIGNED NOT NULL, PRIMARY KEY (lat10, lon10), KEY k_pais (pais)", "")),
        ("serie", ("dia DATE NOT NULL, combo SMALLINT UNSIGNED NOT NULL, " + m +
                   ", PRIMARY KEY (dia, combo)", "")),
        ("dia", ("dia DATE NOT NULL, pais SMALLINT UNSIGNED NOT NULL, " + m + ", PRIMARY KEY (dia, pais)", "")),
        ("pais_tot", ("pais SMALLINT UNSIGNED NOT NULL, combo SMALLINT UNSIGNED NOT NULL, " + m +
                      ", PRIMARY KEY (pais, combo)", "")),
        ("cel_mes", ("mes DATE NOT NULL, lat2 SMALLINT NOT NULL, lon2 SMALLINT NOT NULL, "
                     "pais SMALLINT UNSIGNED NOT NULL, " + m + ", PRIMARY KEY (mes, lat2, lon2, pais)", "")),
        ("cel_tot", ("lat2 SMALLINT NOT NULL, lon2 SMALLINT NOT NULL, pais SMALLINT UNSIGNED NOT NULL, "
                     "combo SMALLINT UNSIGNED NOT NULL, " + m + ", PRIMARY KEY (lat2, lon2, pais, combo)", "")),
        ("fino", ("bloco SMALLINT UNSIGNED NOT NULL, lat10 SMALLINT NOT NULL, lon10 SMALLINT NOT NULL, "
                  "pais SMALLINT UNSIGNED NOT NULL, reg SMALLINT UNSIGNED NOT NULL, " + m +
                  ", PRIMARY KEY (bloco, lat10, lon10)", "")),
        ("ponto", ("bloco SMALLINT UNSIGNED NOT NULL, dia DATE NOT NULL, id " + tipo_id + " NOT NULL, "
                   "lat INT NOT NULL, lon INT NOT NULL, combo SMALLINT UNSIGNED NOT NULL, "
                   "pais SMALLINT UNSIGNED NOT NULL, reg SMALLINT UNSIGNED NOT NULL, frp FLOAT NOT NULL, "
                   "minuto SMALLINT UNSIGNED NOT NULL DEFAULT 0, PRIMARY KEY (bloco, dia, id)", "")),
    ])


def tabelas_existentes(cur):
    cur.execute("SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name LIKE %s", (PRE.replace("_", "\\_") + "%",))
    return {r[0].lower() for r in cur.fetchall()}


def le_meta(cur):
    cur.execute("SELECT chave, valor FROM " + PRE + "meta")
    return dict(cur.fetchall())


def grava_meta(cur, **kv):
    for k, v in kv.items():
        cur.execute("INSERT INTO " + PRE + "meta (chave, valor) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE valor = VALUES(valor)", (k, str(v)))


def apaga_resumos(cur):
    for t in sorted(tabelas_existentes(cur)):
        if t.startswith(PRE):
            cur.execute("DROP TABLE IF EXISTS `" + t + "`")


def amostra_tira(cur, pk, ultimo):
    """Guarda algumas linhas antigas para descobrir depois se a tabela foi recarregada."""
    cur.execute("SELECT MIN(" + pk + ") FROM " + TAB)
    mn = cur.fetchone()[0]
    out = []
    if mn is None:
        return out
    for k in range(0, 17):
        alvo = int(mn) + (int(ultimo) - int(mn)) * k // 16
        cur.execute("SELECT " + pk + ", acq_datetime_utc, latitude, longitude FROM " + TAB +
                    " WHERE " + pk + " <= %s ORDER BY " + pk + " DESC LIMIT 1", (alvo,))
        r = cur.fetchone()
        if r:
            out.append([int(r[0]), str(r[1]), str(r[2]), str(r[3])])
    return out


def amostra_confere(cur, pk, lista):
    for i, dt, la, lo in lista:
        cur.execute("SELECT acq_datetime_utc, latitude, longitude FROM " + TAB + " WHERE " + pk + " = %s", (i,))
        r = cur.fetchone()
        if not r or [str(r[0]), str(r[1]), str(r[2])] != [dt, la, lo]:
            return False
    return True


def situacao(cur, geo, pk, contar=False):
    """Compara os resumos com a firms_tratado. Devolve dict(motivo, ultimo, max_id, novos)."""
    cur.execute("SELECT MIN(" + pk + "), MAX(" + pk + ") FROM " + TAB)
    mn, mx = cur.fetchone()
    mx = int(mx) if mx is not None else 0
    s = dict(motivo=None, ultimo=0, max_id=mx, linhas=0, vazio=mn is None)
    exist = tabelas_existentes(cur)
    if not any(t.startswith(PRE) for t in exist):
        s["motivo"] = "primeira vez"
        return s
    if any(PRE + t not in exist for t in TABELAS):
        s["motivo"] = "falta alguma tabela de resumo"
        return s
    meta = le_meta(cur)
    if meta.get("versao") != VERSAO:
        s["motivo"] = "o formato dos resumos mudou"
        return s
    if meta.get("geo") != geo.assinatura:
        s["motivo"] = "a base do mapa (dashboard_geo.json) mudou"
        return s
    s["ultimo"] = int(meta.get("ultimo_id") or 0)
    s["linhas"] = int(meta.get("linhas") or 0)
    if s["ultimo"] > mx:
        s["motivo"] = "linhas foram removidas da " + TAB
    elif s["ultimo"] and not amostra_confere(cur, pk, json.loads(meta.get("amostra") or "[]")):
        s["motivo"] = "linhas antigas da " + TAB + " mudaram"
    elif contar and s["ultimo"]:
        cur.execute("SELECT COUNT(*) FROM " + TAB + " WHERE " + pk + " <= %s", (s["ultimo"],))
        if int(cur.fetchone()[0]) != s["linhas"]:
            s["motivo"] = "a quantidade de linhas antigas da " + TAB + " mudou"
    if s["motivo"]:
        s["ultimo"] = s["linhas"] = 0
    return s


def _bin(expr):
    """Texto vindo da firms_tratado comparado byte a byte (independe da collation da tabela)."""
    return "CONVERT(LEFT(COALESCE(%s, ''), 40) USING utf8mb4) COLLATE utf8mb4_bin" % expr


LAT10 = "CAST(GREATEST(LEAST(FLOOR(r.latitude * 10), 899), -900) AS SIGNED)"
LON10 = "CAST(GREATEST(LEAST(FLOOR(r.longitude * 10), 1799), -1800) AS SIGNED)"
BLOCO = "(FLOOR(lat10 / 10) + 90) * 360 + FLOOR(lon10 / 10) + 180"
AGG = "COUNT(*), COALESCE(SUM(frp), 0), COALESCE(MAX(frp), 0)"
SOMA = (" ON DUPLICATE KEY UPDATE qtd = qtd + VALUES(qtd), frp_soma = frp_soma + VALUES(frp_soma), "
        "frp_max = GREATEST(frp_max, VALUES(frp_max))")


def preparar(cfg, geo, refazer=False, contar=False, quieto=False):
    """Cria/atualiza as tabelas de apoio. Devolve True se processou alguma coisa."""
    import warnings
    warnings.filterwarnings("ignore", module="pymysql")
    con = conectar(cfg)
    cur = con.cursor()
    pk, tipo_id = inspeciona(cur)
    s = situacao(cur, geo, pk, contar)
    if refazer and not s["motivo"]:
        s["motivo"], s["ultimo"], s["linhas"] = "pedido com --refazer", 0, 0
    if not s["motivo"] and s["ultimo"] >= s["max_id"]:
        if not quieto:
            print("Resumos em dia: %s focos ja processados, nada novo na %s." % (fmt(s["linhas"]), TAB))
        con.close()
        return False
    if s["vazio"]:
        con.close()
        raise Erro("A tabela %s esta vazia. Rode a carga primeiro." % TAB, 503)

    for stmt in ("SET SESSION sql_log_bin = 0",                 # nao lota o log binario com tabelas derivadas
                 "SET SESSION tmp_table_size = 268435456",
                 "SET SESSION max_heap_table_size = 268435456"):   # 256 MB
        try:
            cur.execute(stmt)
        except Exception:
            pass

    do_zero = bool(s["motivo"])
    if do_zero:
        if s["motivo"] != "primeira vez":
            print("Refazendo os resumos do zero (%s)." % s["motivo"])
        apaga_resumos(cur)
    for nome, (cols, extra) in ddl(tipo_id).items():
        cur.execute("CREATE TABLE IF NOT EXISTS %s%s (%s) ENGINE=InnoDB%s" % (PRE, nome, cols, extra))
    if do_zero:
        grava_meta(cur, versao=VERSAO, geo=geo.assinatura, ultimo_id=0, linhas=0, amostra="[]")
        linhas = [(0, 0, None, "Sem pa\u00eds (mar)", "-")]
        for b in geo.brutos:
            e = geo.paises[geo.base(b)]
            nome = e["n"] + (" (parte asi\u00e1tica)" if b >= 1000 else "")
            linhas.append((b, geo.base(b), e["iso"], nome, geo.continentes[geo.cont_de(b)]))
        cur.executemany("INSERT INTO " + PRE + "pais VALUES (%s, %s, %s, %s, %s)", linhas)
        cur.executemany("INSERT INTO " + PRE + "regiao VALUES (%s, %s, %s)",
                        [(i + 1, r[0], r[1]) for i, r in enumerate(geo.regioes)])

    ultimo, max_id, feitas = s["ultimo"], s["max_id"], s["linhas"]
    total_ids = max_id - ultimo
    passo = max(250000, min(2000000, -(-total_ids // 12)))
    nblocos = -(-total_ids // passo)
    # a tabela temporaria de cada bloco fica na memoria quando cabe (bem mais rapido)
    motor = "InnoDB"
    try:
        cur.execute("SELECT @@session.max_heap_table_size")
        if int(cur.fetchone()[0]) >= passo * 96:
            motor = "MEMORY"
    except Exception:
        pass
    if ultimo:
        print("Atualizando os resumos com as linhas novas da %s (ids %s a %s)." % (TAB, fmt(ultimo + 1), fmt(max_id)))
    else:
        print("Preparando os resumos do dashboard a partir da %s (ids ate %s)." % (TAB, fmt(max_id)))
        print("Isso e feito uma vez; depois so as linhas novas sao processadas. Pode demorar; nao feche a janela.")

    cur.execute("SELECT satellite, instrument, confidence_nivel, periodo, tipo_cod, classe_frp, combo FROM " + PRE + "combo")
    combos = {tuple(r[:6]): int(r[6]) for r in cur.fetchall()}
    dims = ", ".join("%s AS d%d" % (e, i) for i, e in enumerate(
        [_bin("r.satellite"), _bin("r.instrument"), _bin("r.confidence_nivel"), _bin("r.periodo"),
         "COALESCE(r.tipo_cod, -1)", _bin("r.classe_frp")]))
    faixa = " WHERE r.%s > %%s AND r.%s <= %%s" % (pk, pk)
    t2 = PRE + "t2"
    cel2 = "FLOOR(lat10 / 5) AS la, FLOOR(lon10 / 5) AS lo"
    cargas = [
        "INSERT INTO {p}ponto (bloco, dia, id, lat, lon, combo, pais, reg, frp, minuto) "
        "SELECT {b} AS b, dia, id, lat, lon, combo, pais, reg, COALESCE(frp, 0), minuto FROM {t} ORDER BY b, dia, id",
        "INSERT INTO {p}fino (bloco, lat10, lon10, pais, reg, qtd, frp_soma, frp_max) "
        "SELECT {b} AS b, lat10, lon10, MAX(pais), MAX(reg), {a} FROM {t} "
        "GROUP BY b, lat10, lon10 ORDER BY b, lat10, lon10" + SOMA,
        "INSERT INTO {p}cel_tot (lat2, lon2, pais, combo, qtd, frp_soma, frp_max) "
        "SELECT {l}, pais, combo, {a} FROM {t} GROUP BY la, lo, pais, combo ORDER BY la, lo, pais, combo" + SOMA,
        "INSERT INTO {p}cel_mes (mes, lat2, lon2, pais, qtd, frp_soma, frp_max) "
        "SELECT dia - INTERVAL (DAYOFMONTH(dia) - 1) DAY AS m, {l}, pais, {a} FROM {t} "
        "GROUP BY m, la, lo, pais ORDER BY m, la, lo, pais" + SOMA,
        "INSERT INTO {p}dia (dia, pais, qtd, frp_soma, frp_max) "
        "SELECT dia, pais, {a} FROM {t} GROUP BY dia, pais ORDER BY dia, pais" + SOMA,
        "INSERT INTO {p}serie (dia, combo, qtd, frp_soma, frp_max) "
        "SELECT dia, combo, {a} FROM {t} GROUP BY dia, combo ORDER BY dia, combo" + SOMA,
        "INSERT INTO {p}pais_tot (pais, combo, qtd, frp_soma, frp_max) "
        "SELECT pais, combo, {a} FROM {t} GROUP BY pais, combo ORDER BY pais, combo" + SOMA,
    ]
    cargas = [c.format(p=PRE, t=t2, b=BLOCO, a=AGG, l=cel2) for c in cargas]

    t0 = time.time()
    k = 0
    a = ultimo
    try:
        while a < max_id:
            b = min(a + passo, max_id)
            k += 1
            # 1) combinacoes presentes neste bloco de ids (e quantas linhas ele tem)
            cur.execute("SELECT " + dims + ", COUNT(*) FROM " + TAB + " r" + faixa +
                        " GROUP BY d0, d1, d2, d3, d4, d5", (a, b))
            n = 0
            novas = []
            for r in cur.fetchall():
                n += int(r[6])
                chave = (r[0], r[1], r[2], r[3], int(r[4]), r[5])
                if chave not in combos:
                    combos[chave] = len(combos) + 1
                    novas.append(chave + (combos[chave],))
            if n:
                if novas:
                    cur.executemany("INSERT INTO " + PRE + "combo (satellite, instrument, confidence_nivel, periodo, "
                                    "tipo_cod, classe_frp, combo) VALUES (%s, %s, %s, %s, %s, %s, %s)", novas)
                # 2) linhas do bloco com a combinacao resolvida; pais/estado vem da grade de 0,1 grau
                cur.execute("DROP TEMPORARY TABLE IF EXISTS " + t2)
                cur.execute("CREATE TEMPORARY TABLE " + t2 + " (id " + tipo_id + " NOT NULL, dia DATE NOT NULL, "
                            "lat INT NOT NULL, lon INT NOT NULL, lat10 SMALLINT NOT NULL, lon10 SMALLINT NOT NULL, "
                            "combo SMALLINT UNSIGNED NOT NULL, pais SMALLINT UNSIGNED NULL, "
                            "reg SMALLINT UNSIGNED NULL, frp DOUBLE NULL, minuto SMALLINT UNSIGNED NOT NULL) ENGINE=" + motor)
                cur.execute(
                    "INSERT INTO " + t2 + " SELECT STRAIGHT_JOIN r." + pk + ", DATE(r.acq_datetime_utc), "
                    "CAST(ROUND(r.latitude * 100000) AS SIGNED), CAST(ROUND(r.longitude * 100000) AS SIGNED), " +
                    LAT10 + ", " + LON10 + ", c.combo, g.pais, g.reg, r.frp_mw, "
                    "HOUR(r.acq_datetime_utc) * 60 + MINUTE(r.acq_datetime_utc) FROM " + TAB + " r "
                    "JOIN " + PRE + "combo c ON c.satellite = " + _bin("r.satellite") +
                    " AND c.instrument = " + _bin("r.instrument") +
                    " AND c.confidence_nivel = " + _bin("r.confidence_nivel") +
                    " AND c.periodo = " + _bin("r.periodo") +
                    " AND c.tipo_cod = COALESCE(r.tipo_cod, -1) AND c.classe_frp = " + _bin("r.classe_frp") +
                    " LEFT JOIN " + PRE + "geo g ON g.lat10 = " + LAT10 + " AND g.lon10 = " + LON10 + faixa, (a, b))
                if cur.rowcount != n:
                    raise Erro("Inconsistencia ao preparar os ids %d-%d: %d linhas lidas, %d classificadas. "
                               "Nada foi gravado para este bloco." % (a + 1, b, n, cur.rowcount), 500)
                # celulas de 0,1 grau vistas pela primeira vez: descobre pais/estado e completa
                cur.execute("SELECT DISTINCT lat10, lon10 FROM " + t2 + " WHERE pais IS NULL")
                lote = []
                for la, lo in cur.fetchall():
                    reg = geo.reg_de(int(la), int(lo))
                    lote.append("(%d,%d,%d,%d)" % (la, lo, geo.pais_de_reg(reg), reg))
                if lote:
                    for j in range(0, len(lote), 20000):
                        cur.execute("INSERT INTO " + PRE + "geo (lat10, lon10, pais, reg) VALUES " + ",".join(lote[j:j + 20000]))
                    cur.execute("UPDATE " + t2 + " t JOIN " + PRE + "geo g ON g.lat10 = t.lat10 AND g.lon10 = t.lon10 "
                                "SET t.pais = g.pais, t.reg = g.reg WHERE t.pais IS NULL")
                # 3) grava tudo deste bloco de uma vez (ou nada, se for interrompido)
                cur.execute("START TRANSACTION")
                for c in cargas:
                    cur.execute(c)
            else:
                cur.execute("START TRANSACTION")
            feitas += n
            grava_meta(cur, ultimo_id=b, linhas=feitas)
            cur.execute("COMMIT")
            dec = time.time() - t0
            falta = dec / (b - ultimo) * (max_id - b)
            print("[%2d/%d] %3d%%  ids ate %-13s %10s focos  %7s decorridos%s" % (
                k, nblocos, 100 * (b - ultimo) // total_ids, fmt(b), fmt(n), dur(dec),
                ("  faltam ~" + dur(falta)) if b < max_id else ""), flush=True)
            a = b
    except KeyboardInterrupt:
        try:
            con.close()
        except Exception:
            pass
        print("\nInterrompido. O que ja foi processado esta salvo; rode de novo para continuar de onde parou.")
        sys.exit(1)

    cur.execute("DROP TEMPORARY TABLE IF EXISTS " + t2)
    grava_meta(cur, amostra=json.dumps(amostra_tira(cur, pk, max_id)),
               atualizado_em=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if do_zero:
        print("Compactando as tabelas...", flush=True)
    for t in TABELAS:
        try:
            # depois de uma carga do zero, regravar as tabelas grandes em ordem deixa-as ~1/3 menores
            cur.execute(("OPTIMIZE TABLE " if do_zero and t in ("ponto", "cel_tot", "fino") else "ANALYZE TABLE ") + PRE + t)
            cur.fetchall()
        except Exception:
            pass
    cur.execute("SELECT MIN(dia), MAX(dia) FROM " + PRE + "serie")
    mn, mx = cur.fetchone()
    con.close()
    print("Pronto em %s: %s focos nos resumos, de %s a %s." % (dur(time.time() - t0), fmt(feitas), mn, mx))
    return True


# ----------------------------------------------------------------------
# Estado do servidor (carregado uma vez ao iniciar) e acesso ao banco
# ----------------------------------------------------------------------
class Estado:
    cfg = None
    geo = None
    pk = "id"
    combos = {}        # id -> (satelite, sensor, confianca, periodo, tipo, classe)
    mn = mx = None     # primeiro e ultimo dia com dados
    peso = {}          # bloco de 1 grau -> quantidade de focos
    blocos = []        # blocos com dados, em ordem
    faixas2 = []       # faixas de lat2 com quantidade parecida de linhas na cel_tot
    dims = {}
    total = 0
    atualizado = ""
    lacunas = []
    tem_bruta = True
    blocos_pais = {}
    livres = []        # conexoes ociosas, reaproveitadas entre consultas
    trava = threading.Lock()


E = Estado


def consulta(sql, args=None, desc=False):
    """Executa uma consulta numa conexao do estoque e devolve todas as linhas."""
    with E.trava:
        con = E.livres.pop() if E.livres else None
    if con is not None:
        try:
            con.ping(reconnect=False)
        except Exception:
            con = None
    if con is None:
        con = conectar(E.cfg)
    try:
        cur = con.cursor()
        cur.execute(sql, args)
        linhas = cur.fetchall()
        d = cur.description
        cur.close()
    except BaseException:
        try:
            con.close()
        except Exception:
            pass
        raise
    with E.trava:
        if len(E.livres) < 2 * PARALELO + 4:
            E.livres.append(con)
            con = None
    if con is not None:
        con.close()
    return (d, linhas) if desc else linhas


def paralelo(sqls):
    """Roda varias consultas ao mesmo tempo (uma conexao para cada); resultados na mesma ordem."""
    if len(sqls) == 1:
        return [consulta(sqls[0])]
    res = [None] * len(sqls)
    erros = []

    def roda(i):
        try:
            res[i] = consulta(sqls[i])
        except Exception as e:
            erros.append(e)

    ths = [threading.Thread(target=roda, args=(i,)) for i in range(len(sqls))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if erros:
        raise erros[0]
    return res


def _ordena(vals, ref):
    return sorted(vals, key=lambda v: (ref.index(v) if v in ref else len(ref), str(v)))


def carrega_estado(cfg, geo):
    E.cfg, E.geo = cfg, geo
    con = conectar(cfg)
    try:
        E.pk = inspeciona(con.cursor())[0]
        E.tem_bruta = True
    except Erro:
        # num servidor so com as tabelas firms_dash_* o dashboard funciona; so o detalhe de cada foco fica de fora
        E.pk, E.tem_bruta = "id", False
    finally:
        con.close()
    E.combos = {int(r[0]): (r[1], r[2], r[3], r[4], int(r[5]), r[6]) for r in consulta(
        "SELECT combo, satellite, instrument, confidence_nivel, periodo, tipo_cod, classe_frp FROM " + PRE + "combo")}
    E.mn, E.mx = consulta("SELECT MIN(dia), MAX(dia) FROM " + PRE + "serie")[0]
    if E.mn is None:
        raise Erro("Os resumos estao vazios. Rode: python dashboard_firms.py --preparar", 503)
    tot = {int(r[0]): (int(r[1]), float(r[2])) for r in consulta(
        "SELECT combo, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma) FROM " + PRE + "pais_tot GROUP BY combo")}
    E.peso = {int(r[0]): int(r[1]) for r in consulta(
        "SELECT bloco, CAST(SUM(qtd) AS UNSIGNED) FROM " + PRE + "fino GROUP BY bloco")}
    E.blocos = sorted(E.peso)
    # faixas de latitude para ler a cel_tot em paralelo
    lats = consulta("SELECT lat2, COUNT(*) FROM " + PRE + "cel_tot GROUP BY lat2 ORDER BY lat2")
    n = sum(int(r[1]) for r in lats)
    k = max(1, min(PARALELO, n // 150000))
    E.faixas2, ini, acc = [], None, 0
    for la, q in lats:
        if ini is None:
            ini = int(la)
        acc += int(q)
        if acc >= n / k and len(E.faixas2) < k - 1:
            E.faixas2.append((ini, int(la)))
            ini, acc = None, 0
    if ini is not None:
        E.faixas2.append((ini, int(lats[-1][0])))
    # periodos sem nenhum dado carregado (mais de 3 dias seguidos), para o grafico nao fingir "zero focos"
    dias = [r[0] for r in consulta("SELECT DISTINCT dia FROM " + PRE + "serie ORDER BY dia")]
    lac = [[a + timedelta(days=1), b - timedelta(days=1)] for a, b in zip(dias, dias[1:]) if (b - a).days > 3]
    if len(lac) > 400:
        lac = sorted(sorted(lac, key=lambda g: g[0] - g[1])[:400], key=lambda g: g[0])
    E.lacunas = lac
    E.atualizado = le_meta_valor("atualizado_em")
    E.total = sum(v[0] for v in tot.values())
    cl = {}
    for cid, c in E.combos.items():
        q, f = tot.get(cid, (0, 0.0))
        a = cl.setdefault(c[5], [0, 0.0])
        a[0] += q
        a[1] += f
    classes = sorted(cl, key=lambda x: (x == "", cl[x][1] / cl[x][0] if cl[x][0] else 0))
    sats = sorted({(c[0], c[1]) for c in E.combos.values()},
                  key=lambda s: (ORD_SAT.index(s[0]) if s[0] in ORD_SAT else len(ORD_SAT), s[0]))
    E.dims = dict(
        conf=_ordena({c[2] for c in E.combos.values()}, ORD_CONF),
        periodo=_ordena({c[3] for c in E.combos.values()}, ORD_PER),
        tipo=sorted({c[4] for c in E.combos.values()}),
        sat=[list(s) for s in sats],
        classe=classes,
    )
    E.blocos_pais = {}


def le_meta_valor(chave):
    r = consulta("SELECT valor FROM " + PRE + "meta WHERE chave = %s", (chave,))
    return (r[0][0] or "") if r else ""


# ----------------------------------------------------------------------
# Filtros
# ----------------------------------------------------------------------
class Filtro:
    pass


def _data(qs, nome):
    v = qs.get(nome, [None])[0]
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        raise Erro("Data inv\u00e1lida em '%s': %s" % (nome, v))


def _conj(qs, nome):
    v = qs.get(nome, [None])[0]
    if v is None:
        return None
    return set(x for x in v.split("|") if x != "")


def le_filtro(qs):
    f = Filtro()
    de, ate = _data(qs, "de"), _data(qs, "ate")
    f.todo = (de is None or de <= E.mn) and (ate is None or ate >= E.mx)
    f.de = E.mn if (de is None or de < E.mn) else de
    f.ate = E.mx if (ate is None or ate > E.mx) else ate
    s = dict(sat=_conj(qs, "sat"), conf=_conj(qs, "conf"), per=_conj(qs, "periodo"),
             tipo=_conj(qs, "tipo"), classe=_conj(qs, "classe"))
    if all(v is None for v in s.values()):
        f.combos = None
    else:
        ids = [cid for cid, c in E.combos.items()
               if (s["sat"] is None or c[0] in s["sat"])
               and (s["conf"] is None or c[2] in s["conf"])
               and (s["per"] is None or c[3] in s["per"])
               and (s["tipo"] is None or str(c[4]) in s["tipo"])
               and (s["classe"] is None or (c[5] or SEM_CLASSE) in s["classe"])]
        f.combos = None if len(ids) == len(E.combos) else sorted(ids)
    f.paises = None
    f.pais_exib = None
    p, ct = qs.get("pais", [None])[0], qs.get("cont", [None])[0]
    if p not in (None, ""):
        try:
            f.pais_exib = int(p)
        except ValueError:
            raise Erro("Pa\u00eds inv\u00e1lido: %s" % p)
        f.paises = [0] if f.pais_exib == 0 else sorted(E.geo.codigos_pais(f.pais_exib))
    elif ct:
        f.paises = sorted(E.geo.codigos_cont(ct))
    f.vazio = (f.combos is not None and not f.combos) or (f.paises is not None and not f.paises) or f.de > f.ate
    # a mesma selecao feita de formas diferentes cai na mesma entrada da memoria
    f.chave = (None if f.todo else (f.de, f.ate), None if f.combos is None else tuple(f.combos),
               None if f.paises is None else tuple(f.paises), f.pais_exib)
    return f


def _lista(nums):
    return ",".join(str(int(n)) for n in nums)


def cond(f, com_pais=True):
    """Trecho SQL com os filtros de combinacao e pais (so inteiros ja validados)."""
    out = ""
    if f.combos is not None:
        if len(f.combos) > len(E.combos) / 2:
            out += " AND combo NOT IN (%s)" % (_lista(sorted(set(E.combos) - set(f.combos))) or "0")
        else:
            out += " AND combo IN (%s)" % _lista(f.combos)
    if com_pais and f.paises is not None:
        out += " AND pais IN (%s)" % _lista(f.paises)
    return out


def _per(f, col="dia"):
    return "" if f.todo else " AND %s BETWEEN '%s' AND '%s'" % (col, f.de.isoformat(), f.ate.isoformat())


def _fim_mes(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def fatias(f):
    """Divide o periodo em meses inteiros (resumo mensal) + dias avulsos nas pontas."""
    de = E.mn.replace(day=1) if f.de <= E.mn else f.de
    ate = _fim_mes(E.mx) if f.ate >= E.mx else f.ate
    m1 = de if de.day == 1 else _fim_mes(de) + timedelta(days=1)
    m2 = (ate if ate == _fim_mes(ate) else ate.replace(day=1) - timedelta(days=1)).replace(day=1)
    if m1 > m2:
        return [("dia", de, ate)]
    out = []
    if de < m1:
        out.append(("dia", de, m1 - timedelta(days=1)))
    out.append(("mes", m1, m2))
    if _fim_mes(m2) < ate:
        out.append(("dia", _fim_mes(m2) + timedelta(days=1), ate))
    return out


def partes(ids):
    """Divide os blocos em grupos com quantidade parecida de focos, para varrer em paralelo."""
    ids = sorted(ids)
    tot = sum(E.peso.get(i, 0) for i in ids)
    k = min(PARALELO, max(1, tot // 150000))
    if k <= 1 or len(ids) <= 1:
        return [ids] if ids else []
    out, grupo, acc = [], [], 0
    for i in ids:
        grupo.append(i)
        acc += E.peso.get(i, 0)
        if acc >= tot / k and len(out) < k - 1:
            out.append(grupo)
            grupo, acc = [], 0
    if grupo:
        out.append(grupo)
    return out


def _blocos_do_pais(cods):
    chave = tuple(cods)
    with E.trava:
        b = E.blocos_pais.get(chave)
    if b is None:
        b = frozenset(int(r[0]) for r in consulta(
            "SELECT DISTINCT " + BLOCO + " FROM " + PRE + "geo WHERE pais IN (" + _lista(cods) + ")"))
        with E.trava:
            E.blocos_pais[chave] = b
    return b


def _alvo(f):
    """Blocos de 1 grau que podem ter focos para este filtro."""
    if f.paises is None or f.paises == [0]:
        return E.blocos
    bp = _blocos_do_pais(f.paises)
    return [i for i in E.blocos if i in bp]


DICA = "/*+ MAX_EXECUTION_TIME(%d) */ " % TEMPO_MAX_MS


def varre(campos, grupo, ids, onde):
    """Agrega a tabela de focos (firms_dash_ponto) em paralelo; devolve uma lista de resultados."""
    sqls = ["SELECT " + DICA + campos + " FROM " + PRE + "ponto WHERE bloco IN (" + _lista(p) + ")" + onde +
            " GROUP BY " + grupo for p in partes(ids)]
    return paralelo(sqls) if sqls else []


# ----------------------------------------------------------------------
# Consultas
# ----------------------------------------------------------------------
def meta():
    return dict(
        intervalo={"min": E.mn, "max": E.mx}, total=E.total, atualizado=E.atualizado,
        dims=E.dims, sem_classe=SEM_CLASSE, limite_pontos=LIMITE_PONTOS, lacunas=E.lacunas,
        combos=[[cid] + list(c) for cid, c in sorted(E.combos.items())],
        banco=E.cfg.database, tabela=TAB,
    )


def resumo(f):
    g = E.geo
    por = dict(conf={k: [0, 0.0] for k in E.dims["conf"]}, per={k: [0, 0.0] for k in E.dims["periodo"]},
               tipo={k: [0, 0.0] for k in E.dims["tipo"]}, sat={tuple(k): [0, 0.0] for k in E.dims["sat"]},
               classe={k: [0, 0.0] for k in E.dims["classe"]})
    paises, conts = {}, {k: [0, 0.0] for k in g.continentes}
    t = dict(total=0, frp=0.0, fmax=0.0, com_frp=0)
    dias = {}

    def soma_combo(cid, q, fs, fm):
        cb = E.combos.get(int(cid))
        if cb is None:
            return
        q, fs, fm = int(q), float(fs or 0), float(fm or 0)
        t["total"] += q
        t["frp"] += fs
        t["fmax"] = max(t["fmax"], fm)
        if cb[5] != "":
            t["com_frp"] += q
        for d, k in ((por["sat"], (cb[0], cb[1])), (por["conf"], cb[2]), (por["per"], cb[3]),
                     (por["tipo"], cb[4]), (por["classe"], cb[5])):
            d[k][0] += q
            d[k][1] += fs

    def soma_pais(p, q, fs, fm):
        p, q, fs, fm = int(p), int(q), float(fs or 0), float(fm or 0)
        a = paises.setdefault(g.base(p), [0, 0.0, 0.0])
        a[0] += q
        a[1] += fs
        a[2] = max(a[2], fm)
        ct = g.cont_de(p)
        if ct in conts:
            conts[ct][0] += q
            conts[ct][1] += fs

    if not f.vazio:
        c, per = cond(f), _per(f)
        ids = _alvo(f)
        sqls, papeis = [], []

        def poe(papel, sql):
            papeis.append(papel)
            sqls.append(sql)

        def poe_ponto(papel, campos, grupo):
            for p in partes(ids):
                poe(papel, "SELECT " + DICA + campos + " FROM " + PRE + "ponto WHERE bloco IN (" + _lista(p) + ")" +
                    per + c + " GROUP BY " + grupo)

        # serie diaria
        if f.paises is None:
            poe("dia", "SELECT dia, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma) FROM " + PRE + "serie WHERE 1 = 1" +
                per + cond(f, False) + " GROUP BY dia")
        elif f.combos is None:
            poe("dia", "SELECT dia, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma) FROM " + PRE + "dia WHERE 1 = 1" +
                per + c + " GROUP BY dia")
        else:
            poe_ponto("dia", "dia, COUNT(*), SUM(frp)", "dia")
        # totais por pais e por combinacao
        if f.todo:
            poe("pc", "SELECT pais, combo, qtd, frp_soma, frp_max FROM " + PRE + "pais_tot WHERE 1 = 1" + c)
        elif f.combos is None and f.paises is None:
            poe("c", "SELECT combo, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma), MAX(frp_max) FROM " + PRE +
                "serie WHERE 1 = 1" + per + " GROUP BY combo")
            poe("p", "SELECT pais, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma), MAX(frp_max) FROM " + PRE +
                "dia WHERE 1 = 1" + per + " GROUP BY pais")
        else:
            poe_ponto("pc", "pais, combo, COUNT(*), SUM(frp), MAX(frp)", "pais, combo")
        for papel, linhas in zip(papeis, paralelo(sqls)):
            if papel == "dia":
                for d, q, fs in linhas:
                    a = dias.setdefault(d, [0, 0.0])
                    a[0] += int(q)
                    a[1] += float(fs or 0)
            elif papel == "c":
                for cid, q, fs, fm in linhas:
                    soma_combo(cid, q, fs, fm)
            elif papel == "p":
                for p, q, fs, fm in linhas:
                    soma_pais(p, q, fs, fm)
            else:
                for p, cid, q, fs, fm in linhas:
                    soma_combo(cid, q, fs, fm)
                    soma_pais(p, q, fs, fm)
    return dict(
        periodo_efetivo={"de": f.de, "ate": f.ate},
        kpis=dict(total=t["total"], frp_soma=t["frp"], frp_max=t["fmax"], com_frp=t["com_frp"],
                  paises=sum(1 for k, v in paises.items() if k and v[0])),
        diario=[[d, v[0], round(v[1], 2)] for d, v in sorted(dias.items())],
        conf=[[k, v[0], v[1]] for k, v in por["conf"].items()],
        periodo=[[k, v[0], v[1]] for k, v in por["per"].items()],
        tipo=[[k, v[0], v[1]] for k, v in por["tipo"].items()],
        sat=[[k[0], k[1], v[0], v[1]] for k, v in por["sat"].items()],
        classe=[[k, v[0], v[1]] for k, v in por["classe"].items()],
        paises=sorted(([k, v[0], v[1], v[2]] for k, v in paises.items() if v[0]), key=lambda r: -r[1]),
        continentes=[[k, v[0], v[1]] for k, v in conts.items()],
    )


def pacote(cab, colunas):
    """Resposta binaria: [uint32 tamanho][cabecalho JSON][colunas tipadas, little-endian]."""
    cab = dict(cab)
    cab["colunas"] = [[c[0], c[1]] for c in colunas]
    partes_ = []
    for _, tipo, vals in colunas:
        a = array(tipo, vals)
        if sys.byteorder == "big":
            a.byteswap()
        partes_.append(a.tobytes())
    h = json.dumps(cab, default=_conv).encode("utf-8")
    h += b" " * ((-(4 + len(h))) % 8)
    return b"".join([struct.pack("<I", len(h)), h] + partes_)


def _pacote_cel(cab, linhas):
    cab["n"] = len(linhas)
    if linhas:
        la, lo, q, fs, fm = zip(*linhas)
    else:
        la = lo = q = fs = fm = ()
    return pacote(cab, [("frp", "d", [float(x or 0) for x in fs]), ("qtd", "I", [int(x) for x in q]),
                        ("max", "f", [float(x or 0) for x in fm]), ("lat", "i", la), ("lon", "i", lo)])


def _blocos_caixa(caixa):
    w, s, e, n = caixa
    r0, r1 = max(0, int(math.floor(s)) + 90), min(179, int(math.ceil(n)) - 1 + 90)
    c0, c1 = max(0, int(math.floor(w)) + 180), min(359, int(math.ceil(e)) - 1 + 180)
    return [r * 360 + c for r in range(r0, r1 + 1) for c in range(c0, c1 + 1) if r * 360 + c in E.peso]


def _caixa(qs):
    v = qs.get("bbox", [None])[0]
    try:
        w, s, e, n = [float(x) for x in v.split(",")]
    except Exception:
        raise Erro("Par\u00e2metro bbox inv\u00e1lido")
    w, e = max(-180.0, w), min(180.0, e)
    s, n = max(-90.0, s), min(90.0, n)
    if not (w < e and s < n):
        raise Erro("Par\u00e2metro bbox inv\u00e1lido")
    return w, s, e, n


CEL = "CAST(FLOOR(lat / %d) AS SIGNED) AS la, CAST(FLOOR(lon / %d) AS SIGNED) AS lo, COUNT(*), SUM(frp), MAX(frp)"


def mapa(f, qs):
    """Camadas do mapa. nivel 0: mundo (0,5 grau); 1: 0,1 grau; 2: grade fina; 3: focos individuais."""
    try:
        nivel = int(qs.get("nivel", ["0"])[0])
    except ValueError:
        raise Erro("Par\u00e2metro nivel inv\u00e1lido")
    c, per = cond(f), _per(f)
    if nivel == 0:
        cab = dict(nivel=0, div=2)
        if f.vazio:
            return _pacote_cel(cab, [])
        if f.todo:
            res = paralelo(["SELECT lat2, lon2, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma), MAX(frp_max) FROM " + PRE +
                            "cel_tot WHERE lat2 BETWEEN %d AND %d" % fx + c + " GROUP BY lat2, lon2"
                            for fx in E.faixas2])
            return _pacote_cel(cab, [r for parte in res for r in parte])
        ids = _alvo(f)
        if f.combos is not None:
            res = varre(CEL % (50000, 50000), "la, lo", ids, per + c)
            return _pacote_cel(cab, [r for parte in res for r in parte])
        sqls = []
        for tipo, a, b in fatias(f):
            if tipo == "mes":
                sqls.append("SELECT lat2, lon2, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma), MAX(frp_max) FROM " + PRE +
                            "cel_mes WHERE mes BETWEEN '%s' AND '%s'" % (a.isoformat(), b.isoformat()) + c +
                            " GROUP BY lat2, lon2")
            else:
                for p in partes(ids):
                    sqls.append("SELECT " + DICA + CEL % (50000, 50000) + " FROM " + PRE + "ponto WHERE bloco IN (" +
                                _lista(p) + ") AND dia BETWEEN '%s' AND '%s'" % (a.isoformat(), b.isoformat()) + c +
                                " GROUP BY la, lo")
        acc = {}
        for parte in paralelo(sqls):
            for la, lo, q, s, m in parte:
                v = acc.get((la, lo))
                if v is None:
                    acc[(la, lo)] = [int(q), float(s or 0), float(m or 0)]
                else:
                    v[0] += int(q)
                    v[1] += float(s or 0)
                    if m and m > v[2]:
                        v[2] = float(m)
        return _pacote_cel(cab, [(k[0], k[1], v[0], v[1], v[2]) for k, v in acc.items()])

    caixa = _caixa(qs)
    ids = _blocos_caixa(caixa)
    if f.paises is not None and ids and f.paises != [0]:
        bp = _blocos_do_pais(f.paises)
        ids = [i for i in ids if i in bp]
    if len(ids) > MAX_BLOCOS:
        raise Erro("\u00c1rea grande demais para este n\u00edvel de detalhe.", 413)
    if nivel == 1:
        cab = dict(nivel=1, div=10, bbox=list(caixa))
        if f.vazio or not ids:
            return _pacote_cel(cab, [])
        if f.todo and f.combos is None:
            res = paralelo(["SELECT lat10, lon10, qtd, frp_soma, frp_max FROM " + PRE + "fino WHERE bloco IN (" +
                            _lista(p) + ")" + c for p in partes(ids)])
        else:
            res = varre(CEL % (10000, 10000), "la, lo", ids, per + c)
        return _pacote_cel(cab, [r for parte in res for r in parte])

    try:
        k = int(qs.get("k", ["100"])[0])
    except ValueError:
        k = 0
    if k not in DIVISOR or k == 10:
        raise Erro("Par\u00e2metro k inv\u00e1lido")
    w, s, e, n = caixa
    onde = (per + " AND lat >= %d AND lat < %d AND lon >= %d AND lon < %d" % (
        round(s * 1e5), round(n * 1e5), round(w * 1e5), round(e * 1e5)) + c)
    if nivel == 3 and not f.vazio and ids:
        linhas = consulta("SELECT " + DICA + "id, lat, lon, DATEDIFF(dia, '1970-01-01'), combo, frp FROM " + PRE +
                          "ponto WHERE bloco IN (" + _lista(ids) + ")" + onde + " LIMIT %d" % (LIMITE_PONTOS + 1))
        if len(linhas) <= LIMITE_PONTOS:
            cab = dict(nivel=3, bbox=list(caixa), n=len(linhas))
            if linhas:
                i, la, lo, d, cb, fr = zip(*linhas)
            else:
                i = la = lo = d = cb = fr = ()
            return pacote(cab, [("id", "d", [float(x) for x in i]), ("lat", "i", la), ("lon", "i", lo),
                                ("dia", "i", d), ("frp", "f", fr), ("combo", "H", cb)])
    cab = dict(nivel=2, div=k, bbox=list(caixa))
    if f.vazio or not ids:
        return _pacote_cel(cab, [])
    dv = DIVISOR[k]
    res = varre(CEL % (dv, dv), "la, lo", ids, onde)
    return _pacote_cel(cab, [r for parte in res for r in parte])


def estados(f):
    """Focos por estado/provincia do pais selecionado."""
    if f.pais_exib is None or f.vazio or not f.paises or f.paises == [0]:
        return dict(estados=[])
    ids = _alvo(f)
    if not ids:
        return dict(estados=[])
    c = cond(f)
    acc = {}
    if f.todo and f.combos is None:
        res = paralelo(["SELECT reg, CAST(SUM(qtd) AS UNSIGNED), SUM(frp_soma), MAX(frp_max) FROM " + PRE +
                        "fino WHERE bloco IN (" + _lista(p) + ")" + c + " GROUP BY reg" for p in partes(ids)])
    else:
        res = varre("reg, COUNT(*), SUM(frp), MAX(frp)", "reg", ids, _per(f) + c)
    for parte in res:
        for r, q, s, m in parte:
            a = acc.setdefault(int(r), [0, 0.0, 0.0])
            a[0] += int(q)
            a[1] += float(s or 0)
            a[2] = max(a[2], float(m or 0))
    return dict(estados=sorted(([r, v[0], v[1], v[2]] for r, v in acc.items()), key=lambda x: -x[1]))


def foco(qs):
    """Todos os campos de um foco (linha original da firms_tratado)."""
    try:
        i = int(float(qs.get("id", [""])[0]))
    except ValueError:
        raise Erro("Par\u00e2metro id inv\u00e1lido")
    if not E.tem_bruta:
        raise Erro("Detalhes do foco indispon\u00edveis: a tabela %s n\u00e3o est\u00e1 neste banco" % TAB, 404)
    d, linhas = consulta("SELECT * FROM " + TAB + " WHERE " + E.pk + " = %s", (i,), desc=True)
    if not linhas:
        raise Erro("Foco n\u00e3o encontrado", 404)
    return {c[0]: (str(v) if isinstance(v, (datetime, date)) else v) for c, v in zip(d, linhas[0])}


def _conv(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()[:10]
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    raise TypeError(type(o))


# ----------------------------------------------------------------------
# Servidor HTTP
# ----------------------------------------------------------------------
class Cache:
    """Respostas ja calculadas. Os dados so mudam quando o preparo roda, entao valem ate o programa fechar."""
    def __init__(self):
        self.d = OrderedDict()
        self.bytes = 0
        self.trava = threading.Lock()

    def get(self, k):
        with self.trava:
            v = self.d.get(k)
            if v is not None:
                self.d.move_to_end(k)
            return v

    def put(self, k, v):
        if len(v[1]) > CACHE_BYTES // 4:
            return
        with self.trava:
            ant = self.d.pop(k, None)
            if ant:
                self.bytes -= len(ant[1])
            self.d[k] = v
            self.bytes += len(v[1])
            while self.bytes > CACHE_BYTES and self.d:
                _, x = self.d.popitem(last=False)
                self.bytes -= len(x[1])


CACHE = Cache()
JSON_T = "application/json; charset=utf-8"
BIN_T = "application/octet-stream"


def _txt(obj):
    return json.dumps(obj, default=_conv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def responde(nome, qs):
    """Calcula (ou pega da memoria) a resposta de /api/<nome>. Devolve (tipo, corpo)."""
    if nome == "meta":
        chave, f = ("meta",), None
    elif nome == "foco":
        chave, f = ("foco", qs.get("id", [""])[0]), None
    elif nome in ("resumo", "mapa", "estados"):
        f = le_filtro(qs)
        chave = (nome,) + f.chave
        if nome == "mapa":
            chave += tuple(qs.get(k, [""])[0] for k in ("nivel", "bbox", "k"))
    else:
        raise Erro("n\u00e3o encontrado", 404)
    hit = CACHE.get(chave)
    if hit:
        return hit
    if nome == "meta":
        r = (JSON_T, _txt(meta()))
    elif nome == "foco":
        r = (JSON_T, _txt(foco(qs)))
    elif nome == "resumo":
        r = (JSON_T, _txt(resumo(f)))
    elif nome == "estados":
        r = (JSON_T, _txt(estados(f)))
    else:
        r = (BIN_T, mapa(f, qs))
    CACHE.put(chave, r)
    return r


ACESSO = {"usuario": "", "senha": ""}      # preenchido em main() a partir de DASHBOARD_USUARIO / DASHBOARD_SENHA
GZ = {}                                     # arquivos estaticos ja comprimidos


def _gz(corpo):
    return gzip.compress(corpo, 6)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dashboard-firms"
    sys_version = ""

    def log_message(self, fmt_, *args):
        pass  # silencioso; erros sao impressos abaixo

    def _enviar(self, status, tipo, corpo, cache="no-store", gz=None):
        try:
            aceita = "gzip" in (self.headers.get("Accept-Encoding") or "")
            if aceita and len(corpo) > 1024 and not tipo.startswith("image/"):
                corpo = gz if gz is not None else _gz(corpo)
                comprimido = True
            else:
                comprimido = False
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", cache)
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("X-Content-Type-Options", "nosniff")
            if comprimido:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionError):
            pass  # o navegador desistiu deste pedido (o usuario ja mudou de zoom/filtro)

    def _arquivo(self, caminho, tipo, cache="no-cache"):
        try:
            st = os.stat(caminho)
            chave = (caminho, st.st_mtime, st.st_size)
            if chave not in GZ:
                with open(caminho, "rb") as f:
                    bruto = f.read()
                GZ[chave] = (bruto, _gz(bruto))
            bruto, comp = GZ[chave]
            self._enviar(200, tipo, bruto, cache, comp)
        except FileNotFoundError:
            self._enviar(500, "text/plain; charset=utf-8",
                         ("%s nao encontrado ao lado de dashboard_firms.py" % os.path.basename(caminho)).encode())

    def _autorizado(self):
        if not ACESSO["senha"]:
            return True
        h = self.headers.get("Authorization") or ""
        if h.startswith("Basic "):
            try:
                u, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
            except Exception:
                u, pw = "", ""
            if hmac.compare_digest(u.encode(), ACESSO["usuario"].encode()) and \
                    hmac.compare_digest(pw.encode(), ACESSO["senha"].encode()):
                return True
        corpo = "Acesso restrito".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Focos de calor", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/saude":            # para o balanceador / monitor (sem senha, sem dados)
            return self._enviar(200, "text/plain; charset=utf-8", b"ok")
        if not self._autorizado():
            return
        if u.path in ("/", "/index.html"):
            self._arquivo(ARQ_HTML, "text/html; charset=utf-8")
        elif u.path == "/dashboard_geo.json":
            self._arquivo(ARQ_GEO, JSON_T, "public, max-age=3600")
        elif u.path.startswith("/api/"):
            self._api(u.path[5:], u.query)
        else:
            self._enviar(404, "text/plain; charset=utf-8", b"nao encontrado")

    def _api(self, nome, query):
        try:
            # keep_blank_values: "conf=" (nenhum marcado) deve dar resultado vazio, nao "tudo"
            tipo, corpo = responde(nome, parse_qs(query, keep_blank_values=True))
        except Erro as e:
            return self._enviar(e.status, JSON_T, _txt({"erro": str(e)}))
        except Exception as e:  # erro de banco, tabela ausente etc.
            msg = str(e)
            if "3024" in msg or "maximum statement execution time" in msg:
                return self._enviar(504, JSON_T, _txt({"erro": "A consulta demorou demais.", "lento": True}))
            if "1146" in msg or "doesn't exist" in msg:
                msg, status = "Tabelas de resumo n\u00e3o encontradas. Rode: python dashboard_firms.py --preparar", 503
            else:
                status = 500
                print("ERRO em /api/%s: %s" % (nome, e), file=sys.stderr)
            return self._enviar(status, JSON_T, _txt({"erro": msg[:300]}))
        self._enviar(200, tipo, corpo)


def aquece():
    """Deixa pronta a visao inicial (sem filtros) enquanto o navegador abre."""
    try:
        responde("resumo", {})
        responde("mapa", {"nivel": ["0"]})
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser(description="Dashboard cartografico local dos focos de calor")
    p.add_argument("--host", default=os.environ.get("MYSQL_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", 3306)))
    p.add_argument("--user", default=os.environ.get("MYSQL_USER", "root"))
    p.add_argument("--database", default=os.environ.get("MYSQL_DATABASE", "area_queimada"))
    p.add_argument("--porta", type=int, default=int(os.environ.get("PORT", 8000)), help="porta do dashboard (padrao 8000)")
    p.add_argument("--ouvir", default=os.environ.get("DASHBOARD_ENDERECO", "127.0.0.1"),
                   help="endereco em que o dashboard atende (padrao 127.0.0.1 = so este computador; "
                        "0.0.0.0 = toda a rede)")
    p.add_argument("--preparar", action="store_true", help="cria/atualiza as tabelas de resumo e sai")
    p.add_argument("--refazer", action="store_true", help="apaga os resumos e refaz tudo do zero, e sai")
    p.add_argument("--apagar", action="store_true", help="remove as tabelas firms_dash_* e sai")
    p.add_argument("--sem-preparo", action="store_true", help="nao verificar/atualizar os resumos ao abrir")
    p.add_argument("--sem-navegador", action="store_true", help="nao abrir o navegador sozinho")
    a = p.parse_args()

    try:
        if a.apagar:
            con = conectar(a)
            apaga_resumos(con.cursor())
            con.close()
            print("Tabelas %s* removidas do banco %s." % (PRE, a.database))
            return
        geo = Geo(ARQ_GEO)
        if a.preparar or a.refazer:
            preparar(a, geo, refazer=a.refazer, contar=True)
            print("Agora rode: python dashboard_firms.py")
            return
        if not os.path.exists(ARQ_HTML):
            raise Erro("dashboard.html nao encontrado ao lado de dashboard_firms.py", 500)
        if not a.sem_preparo:
            con = conectar(a)
            cur = con.cursor()
            cur.execute("SHOW TABLES LIKE %s", (TAB,))
            tem_bruta = cur.fetchone() is not None
            con.close()
            if tem_bruta:
                preparar(a, geo, quieto=True)
        carrega_estado(a, geo)
    except Erro as e:
        sys.exit("ERRO: %s" % e)

    ACESSO["usuario"] = os.environ.get("DASHBOARD_USUARIO", "firms")
    ACESSO["senha"] = os.environ.get("DASHBOARD_SENHA", "")
    local = a.ouvir in ("127.0.0.1", "localhost", "::1")
    if not local and not ACESSO["senha"]:
        print("AVISO: o dashboard esta aberto para a rede SEM senha. Defina DASHBOARD_SENHA para exigir login.")
    try:
        srv = ThreadingHTTPServer((a.ouvir, a.porta), Handler)
    except OSError:
        sys.exit("ERRO: a porta %d ja esta em uso (o dashboard ja esta aberto em outra janela?). "
                 "Feche a outra janela ou use --porta %d." % (a.porta, a.porta + 1))
    srv.daemon_threads = True
    threading.Thread(target=aquece, daemon=True).start()
    url = "http://%s:%d" % ("127.0.0.1" if a.ouvir in ("0.0.0.0", "::") else a.ouvir, a.porta)
    print("Dashboard em %s  (%s focos, de %s a %s)  -  Ctrl+C para encerrar" % (url, fmt(E.total), E.mn, E.mx))
    if not E.tem_bruta:
        print("Aviso: a tabela %s nao esta neste banco; o detalhe de cada foco fica indisponivel." % TAB)
    if not local:
        print("Aberto para a rede na porta %d%s." % (a.porta, " (com senha)" if ACESSO["senha"] else ""))
    if not a.sem_navegador and local:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
