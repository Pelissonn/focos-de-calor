#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
carregar_csv.py - Carrega os CSV baixados da NASA FIRMS no MySQL (tabela firms_tratado).

Como usar (na pasta do projeto):
    pip install pymysql
    python ferramentas/carregar_csv.py caminho/fire_archive_*.csv caminho/fire_nrt_*.csv
    python ferramentas/carregar_csv.py pasta_com_csvs/            (todos os .csv da pasta)
    python ferramentas/carregar_csv.py arquivo.zip                (o .zip baixado do FIRMS)
    python ferramentas/carregar_csv.py arquivo.zip --temp D:/tmp  (descompacta em outro disco)

Depois rode ferramentas/exportar_dados.py para atualizar o dashboard.

O que faz
  - Cria o banco (padrao area_queimada) e a tabela firms_tratado, se nao existirem.
  - Le cada CSV com LOAD DATA LOCAL INFILE (rapido: milhoes de linhas por minuto) e ja grava
    as colunas tratadas que o dashboard usa:
        acq_datetime_utc  data + hora da passagem (UTC)
        confidence_nivel  Baixa / Nominal / Alta (VIIRS l/n/h; MODIS 0-29 / 30-79 / 80-100)
        periodo           Dia / Noite (coluna daynight)
        tipo_cod          coluna type (0 vegetacao, 1 vulcao, 2 fonte estatica, 3 offshore);
                          NULL nos arquivos NRT, que nao trazem essa coluna
        frp_mw, classe_frp  potencia radiativa e a classe dela
        fonte, arquivo    archive / nrt e o nome do arquivo de origem
  - Cada arquivo e registrado na tabela firms_carga: rodar de novo com o mesmo arquivo nao duplica
    nada (use --recarregar para apagar e carregar de novo).
  - Ao carregar um arquivo "archive" (dados definitivos), apaga os focos "nrt" (provisorios) do mesmo
    satelite dentro do periodo coberto por ele, como recomenda a NASA. Desligue com --manter-nrt.

Conexao: padrao localhost:3306, root sem senha (opcoes --host, --port, --user, --database;
senha pela variavel MYSQL_PASSWORD).
"""
import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile

TAB = "firms_tratado"
CARGA = "firms_carga"

# nomes das colunas do FIRMS -> coluna da firms_tratado (VIIRS usa bright_ti4/ti5, MODIS brightness/bright_t31)
APELIDOS = {"bright_ti4": "brightness", "bright_ti5": "bright_t31"}
SIMPLES = {"latitude", "longitude", "brightness", "scan", "track", "satellite", "instrument", "version", "bright_t31"}
CLASSES = [(2, "Muito baixa (< 2 MW)"), (5, "Baixa (2–5 MW)"), (20, "Moderada (5–20 MW)"),
           (100, "Alta (20–100 MW)"), (None, "Muito alta (≥ 100 MW)")]

DDL = """CREATE TABLE IF NOT EXISTS {t} (
  id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
  latitude         DOUBLE NOT NULL,
  longitude        DOUBLE NOT NULL,
  brightness       FLOAT NULL COMMENT 'temperatura de brilho (K): canal I4 do VIIRS ou 21/22 do MODIS',
  scan             FLOAT NULL,
  track            FLOAT NULL,
  acq_date         DATE NOT NULL,
  acq_time         SMALLINT UNSIGNED NOT NULL COMMENT 'HHMM em UTC',
  acq_datetime_utc DATETIME NOT NULL,
  satellite        VARCHAR(10) NOT NULL,
  instrument       VARCHAR(10) NOT NULL,
  confidence       VARCHAR(5) NULL COMMENT 'valor original (l/n/h ou 0-100)',
  confidence_nivel VARCHAR(10) NOT NULL,
  version          VARCHAR(12) NULL,
  bright_t31       FLOAT NULL,
  frp_mw           FLOAT NULL,
  classe_frp       VARCHAR(30) NOT NULL,
  daynight         CHAR(1) NULL,
  periodo          VARCHAR(6) NOT NULL,
  tipo_cod         TINYINT NULL,
  fonte            VARCHAR(8) NOT NULL COMMENT 'archive (definitivo) ou nrt (tempo quase real)',
  arquivo          VARCHAR(120) NOT NULL,
  PRIMARY KEY (id),
  KEY k_data (acq_date, satellite),
  KEY k_arquivo (arquivo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

DDL_CARGA = """CREATE TABLE IF NOT EXISTS {c} (
  arquivo      VARCHAR(120) NOT NULL,
  tamanho      BIGINT UNSIGNED NOT NULL,
  fonte        VARCHAR(8) NOT NULL,
  linhas       INT UNSIGNED NOT NULL,
  data_min     DATE NULL,
  data_max     DATE NULL,
  nrt_apagados INT UNSIGNED NOT NULL DEFAULT 0,
  carregado_em DATETIME NOT NULL,
  segundos     INT UNSIGNED NOT NULL,
  PRIMARY KEY (arquivo)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def fmt(n):
    return "{:,}".format(int(n)).replace(",", ".")


def classe_sql(col):
    partes = ["WHEN %s IS NULL THEN ''" % col]
    for lim, nome in CLASSES:
        partes.append(("WHEN %s < %s THEN '%s'" % (col, lim, nome)) if lim is not None else ("ELSE '%s'" % nome))
    return "CASE " + " ".join(partes) + " END"


def conectar(a, banco=True):
    import pymysql
    try:
        return pymysql.connect(host=a.host, port=a.port, user=a.user, password=os.environ.get("MYSQL_PASSWORD", ""),
                               database=a.database if banco else None, charset="utf8mb4", autocommit=True,
                               local_infile=True)
    except pymysql.err.OperationalError as e:
        cod = e.args[0] if e.args else 0
        if cod in (2002, 2003, 2005):
            sys.exit("Nao consegui conectar ao MySQL em %s:%s. O servico do MySQL (WAMP) esta ligado?" % (a.host, a.port))
        if cod == 1045:
            sys.exit("O MySQL recusou o usuario '%s'. Se ele tem senha, defina a variavel MYSQL_PASSWORD." % a.user)
        raise


def lista_arquivos(entradas, tmp):
    """Expande pastas e .zip em uma lista de CSVs."""
    saida = []
    for e in entradas:
        if os.path.isdir(e):
            saida += sorted(os.path.join(e, f) for f in os.listdir(e) if f.lower().endswith(".csv"))
        elif e.lower().endswith(".zip"):
            with zipfile.ZipFile(e) as z:
                for nome in z.namelist():
                    base = os.path.basename(nome)
                    if base.lower().endswith(".csv"):
                        destino = os.path.join(tmp, base)
                        with z.open(nome) as src, open(destino, "wb") as dst:
                            shutil.copyfileobj(src, dst, 16 * 1024 * 1024)
                        saida.append(destino)
        elif os.path.isfile(e):
            saida.append(e)
        else:
            sys.exit("Nao encontrei: %s" % e)
    if not saida:
        sys.exit("Nenhum arquivo .csv encontrado.")
    # archive antes de nrt: os dados definitivos entram primeiro
    return sorted(saida, key=lambda f: ("nrt" in os.path.basename(f).lower(), os.path.basename(f)))


def cabecalho(caminho):
    with open(caminho, "rb") as f:
        linha = f.readline()
    fim = "\\r\\n" if linha.endswith(b"\r\n") else "\\n"
    cols = next(csv.reader(io.StringIO(linha.decode("utf-8-sig").strip())))
    return [c.strip().lower() for c in cols], fim


def monta_load(caminho, cols, fim, fonte, arquivo):
    alvo, tem = [], set()
    for c in cols:
        c2 = APELIDOS.get(c, c)
        if c2 in SIMPLES and c2 not in tem:
            alvo.append("@v_" + c2)
            tem.add(c2)
        elif c in ("acq_date", "acq_time", "confidence", "frp", "daynight", "type"):
            alvo.append("@" + c)
            tem.add(c)
        else:
            alvo.append("@ignorar")
    faltam = {"latitude", "longitude", "acq_date", "acq_time", "satellite", "instrument"} - tem
    if faltam:
        raise ValueError("colunas obrigatorias ausentes: " + ", ".join(sorted(faltam)))
    nulo = lambda v: "NULLIF(TRIM(%s), '')" % v
    hhmm = "LPAD(TRIM(@acq_time), 4, '0')"
    sets = ["%s = %s" % (c, nulo("@v_" + c)) for c in sorted(SIMPLES & tem)]
    sets += [
        "acq_date = @acq_date",
        "acq_time = CAST(TRIM(@acq_time) AS UNSIGNED)",
        "acq_datetime_utc = TIMESTAMP(@acq_date, MAKETIME(LEFT(%s, 2), RIGHT(%s, 2), 0))" % (hhmm, hhmm),
    ]
    if "confidence" in tem:
        c = "LOWER(TRIM(@confidence))"
        sets += ["confidence = %s" % nulo("@confidence"),
                 "confidence_nivel = CASE WHEN %s IN ('l', 'low') THEN 'Baixa' WHEN %s IN ('n', 'nominal') THEN 'Nominal' "
                 "WHEN %s IN ('h', 'high') THEN 'Alta' WHEN %s REGEXP '^[0-9]+$' THEN "
                 "(CASE WHEN CAST(%s AS UNSIGNED) < 30 THEN 'Baixa' WHEN CAST(%s AS UNSIGNED) < 80 THEN 'Nominal' ELSE 'Alta' END) "
                 "ELSE 'Nominal' END" % (c, c, c, c, c, c)]
    else:
        sets.append("confidence_nivel = 'Nominal'")
    if "frp" in tem:
        sets += ["frp_mw = %s" % nulo("@frp"), "classe_frp = " + classe_sql(nulo("@frp") + " + 0")]
    else:
        sets.append("classe_frp = ''")
    if "daynight" in tem:
        sets += ["daynight = %s" % nulo("@daynight"),
                 "periodo = IF(UPPER(TRIM(@daynight)) = 'N', 'Noite', 'Dia')"]
    else:
        sets.append("periodo = IF(HOUR(TIMESTAMP(@acq_date, MAKETIME(LEFT(%s, 2), 0, 0))) BETWEEN 6 AND 17, 'Dia', 'Noite')" % hhmm)
    sets.append("tipo_cod = %s" % (nulo("@type") if "type" in tem else "NULL"))
    sets += ["fonte = '%s'" % fonte, "arquivo = '%s'" % arquivo.replace("'", "''")]
    caminho_sql = os.path.abspath(caminho).replace("\\", "/").replace("'", "\\'")
    return ("LOAD DATA LOCAL INFILE '%s' INTO TABLE %s CHARACTER SET utf8mb4 "
            "FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '\"' LINES TERMINATED BY '%s' IGNORE 1 LINES (%s) SET %s"
            % (caminho_sql, TAB, fim, ", ".join(alvo), ", ".join(sets)))


def main():
    p = argparse.ArgumentParser(description="Carrega CSVs da NASA FIRMS no MySQL (tabela firms_tratado)")
    p.add_argument("entradas", nargs="+", help="arquivos .csv, pastas ou o .zip baixado do FIRMS")
    p.add_argument("--host", default=os.environ.get("MYSQL_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", 3306)))
    p.add_argument("--user", default=os.environ.get("MYSQL_USER", "root"))
    p.add_argument("--database", default=os.environ.get("MYSQL_DATABASE", "area_queimada"))
    p.add_argument("--recarregar", action="store_true", help="apaga e carrega de novo arquivos ja carregados")
    p.add_argument("--temp", help="pasta para descompactar o .zip (padrao: a pasta temporaria do sistema)")
    p.add_argument("--manter-nrt", action="store_true", help="nao apaga focos NRT cobertos por arquivos archive")
    a = p.parse_args()
    try:
        import pymysql  # noqa: F401
    except ImportError:
        sys.exit("Falta o driver. Instale com: pip install pymysql")

    con = conectar(a, banco=False)
    cur = con.cursor()
    cur.execute("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4" % a.database.replace("`", ""))
    cur.execute("USE `%s`" % a.database.replace("`", ""))
    cur.execute(DDL.format(t=TAB))
    cur.execute(DDL_CARGA.format(c=CARGA))
    cur.execute("SELECT @@GLOBAL.local_infile")
    if not int(cur.fetchone()[0]):
        try:
            cur.execute("SET GLOBAL local_infile = 1")
        except Exception:
            sys.exit("O MySQL esta com local_infile desligado e o usuario '%s' nao pode liga-lo. Rode no MySQL:\n"
                     "  SET GLOBAL local_infile = 1;" % a.user)
    for s in ("SET SESSION sql_log_bin = 0", "SET SESSION unique_checks = 0"):
        try:
            cur.execute(s)
        except Exception:
            pass

    tmp = tempfile.mkdtemp(prefix="firms_csv_", dir=a.temp)
    try:
        arquivos = lista_arquivos(a.entradas, tmp)
        t_geral, total = time.time(), 0
        for caminho in arquivos:
            nome = os.path.basename(caminho)
            tamanho = os.path.getsize(caminho)
            fonte = "nrt" if "nrt" in nome.lower() else "archive"
            cur.execute("SELECT tamanho, linhas FROM " + CARGA + " WHERE arquivo = %s", (nome,))
            ja = cur.fetchone()
            if ja and not a.recarregar:
                print("%s: ja carregado (%s linhas), pulando. Use --recarregar para carregar de novo." % (nome, fmt(ja[1])))
                continue
            if ja:
                print("%s: apagando a carga anterior..." % nome, flush=True)
                cur.execute("DELETE FROM " + TAB + " WHERE arquivo = %s", (nome,))
                cur.execute("DELETE FROM " + CARGA + " WHERE arquivo = %s", (nome,))
            try:
                cols, fim = cabecalho(caminho)
                sql = monta_load(caminho, cols, fim, fonte, nome)
            except (ValueError, StopIteration) as e:
                print("%s: ignorado (%s)" % (nome, e))
                continue
            print("%s: carregando %.0f MB (%s)..." % (nome, tamanho / 1e6, fonte), flush=True)
            t0 = time.time()
            cur.execute("START TRANSACTION")
            cur.execute(sql)
            n = cur.rowcount
            cur.execute("SHOW WARNINGS LIMIT 5")
            avisos = cur.fetchall()
            cur.execute("SELECT MIN(acq_date), MAX(acq_date) FROM " + TAB + " WHERE arquivo = %s", (nome,))
            dmin, dmax = cur.fetchone()
            apagados = 0
            if fonte == "archive" and not a.manter_nrt and dmin:
                cur.execute("SELECT DISTINCT satellite FROM " + TAB + " WHERE arquivo = %s", (nome,))
                sats = [r[0] for r in cur.fetchall()]
                cur.execute("DELETE FROM " + TAB + " WHERE fonte = 'nrt' AND acq_date BETWEEN %s AND %s AND satellite IN ("
                            + ",".join(["%s"] * len(sats)) + ")", [dmin, dmax] + sats)
                apagados = cur.rowcount
            seg = int(time.time() - t0)
            cur.execute("INSERT INTO " + CARGA + " VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s)",
                        (nome, tamanho, fonte, n, dmin, dmax, apagados, seg))
            cur.execute("COMMIT")
            total += n
            print("  %s linhas de %s a %s em %ds%s" % (fmt(n), dmin, dmax, seg,
                  (" · %s focos NRT substituidos pelos definitivos" % fmt(apagados)) if apagados else ""), flush=True)
            for av in avisos:
                print("  aviso do MySQL: %s" % (av[2],))
        cur.execute("SELECT COUNT(*), MIN(acq_date), MAX(acq_date) FROM " + TAB)
        n, mn, mx = cur.fetchone()
        print("\nPronto em %ds: %s linhas novas. A %s tem %s focos, de %s a %s."
              % (time.time() - t_geral, fmt(total), TAB, fmt(n), mn, mx))
        print("Agora atualize o dashboard: python ferramentas/exportar_dados.py")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        con.close()


if __name__ == "__main__":
    main()
