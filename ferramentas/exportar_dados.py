#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exportar_dados.py - Gera os arquivos de dados do app Streamlit a partir do MySQL (rode no SEU computador).

O Streamlit Community Cloud roda na nuvem e nao enxerga o MySQL do seu PC. Por isso os dados vao
para o GitHub em arquivos Parquet (compactos), dentro da pasta dados/focos/ do repositorio.

Como usar (na pasta do repositorio):
    pip install pymysql pyarrow duckdb
    python ferramentas/exportar_dados.py

O que acontece:
  1. Atualiza as tabelas de resumo firms_dash_* do banco (o mesmo preparo do dashboard_firms.py;
     a firms_tratado nunca e alterada).
  2. Grava um registro compacto por foco em dados/focos/*.parquet (um ou mais arquivos por ano,
     cada um com menos de 100 MB, que e o limite do GitHub).
  3. Se o total passar de --limite-mb (padrao 700 MB), troca os focos individuais por uma grade
     de 0,1 grau (cerca de 11 km) por dia - bem menor, e o mapa continua detalhado.
  4. Grava dados/info.json (periodo, satelites, combinacoes etc.).
Depois e so fazer commit e push (veja o README.md).

Conexao: padrao localhost, root sem senha, banco area_queimada (opcoes --host, --port, --user,
--database; senha pela variavel MYSQL_PASSWORD).
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
DESTINO = os.path.join(RAIZ, "dados", "focos")
INFO = os.path.join(RAIZ, "dados", "info.json")
sys.path.insert(0, AQUI)

MAX_LINHAS_ARQUIVO = 6_000_000      # ~60-80 MB por arquivo, abaixo do limite de 100 MB do GitHub
LOTE = 250_000


def fmt(n):
    return "{:,}".format(int(n)).replace(",", ".")


def tamanho(pasta):
    return sum(os.path.getsize(os.path.join(pasta, f)) for f in os.listdir(pasta) if f.endswith(".parquet"))


def main():
    p = argparse.ArgumentParser(description="Exporta os focos do MySQL para o app Streamlit")
    p.add_argument("--host", default=os.environ.get("MYSQL_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", 3306)))
    p.add_argument("--user", default=os.environ.get("MYSQL_USER", "root"))
    p.add_argument("--database", default=os.environ.get("MYSQL_DATABASE", "area_queimada"))
    p.add_argument("--modo", choices=["auto", "focos", "grade"], default="auto",
                   help="focos = um registro por foco; grade = celulas de 0,1 grau por dia; auto = focos se couber")
    p.add_argument("--limite-mb", type=int, default=700, help="tamanho maximo dos dados no repositorio (padrao 700)")
    p.add_argument("--desde", help="exporta so a partir desta data (AAAA-MM-DD), para diminuir o tamanho")
    a = p.parse_args()

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        import duckdb
    except ImportError:
        sys.exit("Faltam bibliotecas. Instale com: pip install pymysql pyarrow duckdb")
    import dashboard_firms as D

    t0 = time.time()
    try:
        geo = D.Geo(os.path.join(AQUI, "dashboard_geo.json"))
        D.preparar(a, geo, quieto=False)
        con = D.conectar(a)
    except D.Erro as e:
        sys.exit("ERRO: %s" % e)
    cur = con.cursor()
    pre = D.PRE
    cur.execute("SELECT combo, satellite, instrument, confidence_nivel, periodo, tipo_cod, classe_frp FROM " + pre + "combo ORDER BY combo")
    combos = [[int(r[0]), r[1], r[2], r[3], r[4], int(r[5]), r[6]] for r in cur.fetchall()]
    filtro = ""
    if a.desde:
        try:
            filtro = " WHERE dia >= '%s'" % date.fromisoformat(a.desde).isoformat()
        except ValueError:
            sys.exit("Data invalida em --desde (use AAAA-MM-DD)")
    cur.execute("SELECT COUNT(*), MIN(dia), MAX(dia) FROM " + pre + "ponto" + filtro)
    total, mn, mx = cur.fetchone()
    total = int(total)
    if not total:
        sys.exit("Nenhum foco nas tabelas de resumo.")
    fontes = []
    try:   # resumo das cargas feitas por carregar_csv.py (se existir)
        cur.execute("SELECT arquivo, fonte, linhas, data_min, data_max, carregado_em FROM firms_carga ORDER BY data_min")
        fontes = [dict(arquivo=r[0], fonte=r[1], linhas=int(r[2]), de=str(r[3]), ate=str(r[4]), em=str(r[5])) for r in cur.fetchall()]
    except Exception:
        pass
    cur.execute("SELECT DISTINCT dia FROM " + pre + "serie" + filtro + " ORDER BY dia")
    dias = [r[0] for r in cur.fetchall()]
    lacunas = [[(x + timedelta(days=1)).isoformat(), (y - timedelta(days=1)).isoformat()]
               for x, y in zip(dias, dias[1:]) if (y - x).days > 3]

    # 1) focos individuais, um conjunto de arquivos por ano (temporario)
    tmp = DESTINO + "_novo"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    print("Exportando %s focos..." % fmt(total), flush=True)
    esquema = pa.schema([("dia", pa.date32()), ("lat", pa.int32()), ("lon", pa.int32()), ("combo", pa.uint16()),
                         ("pais", pa.uint16()), ("reg", pa.uint16()), ("frp", pa.float32()),
                         ("minuto", pa.uint16())])
    escritores, buf, lidas = {}, {}, 0

    def grava(ano, forcar=False):
        b = buf.get(ano)
        if not b or (len(b[0]) < LOTE and not forcar):
            return
        e = escritores.get(ano)
        if e is None or e[1] >= MAX_LINHAS_ARQUIVO:
            if e is not None:
                e[0].close()
            parte = (e[2] + 1) if e is not None else 1
            caminho = os.path.join(tmp, "focos_%d_%d.parquet" % (ano, parte))
            e = [pq.ParquetWriter(caminho, esquema, compression="zstd", compression_level=9), 0, parte]
            escritores[ano] = e
        dia = pa.array(b[0], pa.int32()).cast(pa.date32())
        t = pa.table([dia, pa.array(b[1], pa.int32()), pa.array(b[2], pa.int32()), pa.array(b[3], pa.uint16()),
                      pa.array(b[4], pa.uint16()), pa.array(b[5], pa.uint16()), pa.array(b[6], pa.float32()),
                      pa.array(b[7], pa.uint16())], schema=esquema)
        e[0].write_table(t, row_group_size=200_000)
        e[1] += len(b[0])
        buf[ano] = None

    import pymysql
    scur = con.cursor(pymysql.cursors.SSCursor)
    # ordem da chave primaria (regiao de 1 grau, dia): leitura rapida e arquivos agrupados no espaco
    scur.execute("SELECT DATEDIFF(dia, '1970-01-01'), YEAR(dia), lat, lon, combo, pais, reg, frp, minuto FROM " + pre + "ponto" + filtro)
    while True:
        linhas = scur.fetchmany(50_000)
        if not linhas:
            break
        for d, ano, la, lo, cb, pa_, rg, fr, mi in linhas:
            b = buf.get(ano)
            if b is None:
                b = buf[ano] = ([], [], [], [], [], [], [], [])
            b[0].append(d); b[1].append(la); b[2].append(lo); b[3].append(cb)
            b[4].append(pa_); b[5].append(rg); b[6].append(fr); b[7].append(mi)
            if len(b[0]) >= LOTE:
                grava(ano)
        lidas += len(linhas)
        if lidas % 1_000_000 < 50_000:
            print("  %s de %s (%d%%)" % (fmt(lidas), fmt(total), 100 * lidas // total), flush=True)
    scur.close()
    # 2) qualidade dos dados: uma leitura da tabela original (nulos, valores fora da faixa, origem)
    print("Conferindo a qualidade dos dados da %s..." % D.TAB, flush=True)
    qualidade = {}
    try:
        cur.execute("SELECT COUNT(*), SUM(latitude IS NULL OR longitude IS NULL), SUM(acq_datetime_utc IS NULL), "
                    "SUM(frp_mw IS NULL), SUM(frp_mw < 0), SUM(confidence_nivel IS NULL OR confidence_nivel = ''), "
                    "SUM(tipo_cod IS NULL), SUM(latitude NOT BETWEEN -90 AND 90 OR longitude NOT BETWEEN -180 AND 180) "
                    "FROM " + D.TAB)
        r = [int(x or 0) for x in cur.fetchone()]
        qualidade = dict(linhas_origem=r[0], sem_coordenada=r[1], sem_data=r[2], frp_nulo=r[3], frp_negativo=r[4],
                         sem_confianca=r[5], tipo_nulo=r[6], coordenada_invalida=r[7], linhas_resumo=total)
    except Exception as e:
        print("  (nao consegui conferir a qualidade: %s)" % e)
    for ano in list(buf):
        grava(ano, forcar=True)
    for e in escritores.values():
        e[0].close()
    con.close()

    mb = tamanho(tmp) / 1e6
    modo = a.modo
    if modo == "auto":
        modo = "focos" if mb <= a.limite_mb else "grade"
        if modo == "grade":
            print("Os focos individuais ocupam %.0f MB (acima de %d MB): usando a grade de 0,1 grau por dia." % (mb, a.limite_mb))
    if modo == "grade":
        tmp2 = tmp + "_grade"
        shutil.rmtree(tmp2, ignore_errors=True)
        os.makedirs(tmp2)
        db = duckdb.connect()
        for f in sorted(os.listdir(tmp)):
            src = os.path.join(tmp, f).replace("\\", "/")
            dst = os.path.join(tmp2, f.replace("focos_", "grade_")).replace("\\", "/")
            db.execute(f"""COPY (
                SELECT dia, CAST(FLOOR(lat / 10000) * 10000 AS INTEGER) AS lat, CAST(FLOOR(lon / 10000) * 10000 AS INTEGER) AS lon,
                       combo, pais, reg, CAST(COUNT(*) AS UINTEGER) AS qtd, CAST(SUM(frp) AS FLOAT) AS frp, MAX(frp) AS frp_max
                FROM read_parquet('{src}') GROUP BY ALL ORDER BY lat, lon, dia
            ) TO '{dst}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)""")
        shutil.rmtree(tmp)
        tmp = tmp2
        mb = tamanho(tmp) / 1e6
        if mb > 950:
            print("AVISO: mesmo em grade os dados ocupam %.0f MB; o GitHub pode recusar repositorios desse tamanho." % mb)

    if modo == "focos" and qualidade:
        # duplicados: mesmo instante, coordenada (1e-5 grau) e combinacao satelite/sensor/categorias
        db = duckdb.connect()
        src = os.path.join(tmp, "*.parquet").replace("\\", "/")
        n, dist = db.execute(f"SELECT COUNT(*), COUNT(DISTINCT (dia, minuto, lat, lon, combo)) FROM read_parquet('{src}')").fetchone()
        qualidade["duplicados"] = int(n - dist)
        db.close()
    maior = max(os.path.getsize(os.path.join(tmp, f)) for f in os.listdir(tmp)) / 1e6
    shutil.rmtree(DESTINO, ignore_errors=True)
    os.replace(tmp, DESTINO)
    arquivos = sorted(f for f in os.listdir(DESTINO) if f.endswith(".parquet"))
    info = dict(
        modo=modo, gerado_em=datetime.now().strftime("%Y-%m-%d %H:%M"), banco=a.database, tabela=D.TAB,
        total=total, intervalo=dict(min=str(mn), max=str(mx)), lacunas=lacunas, arquivos=arquivos,
        combos=combos, fontes=fontes, qualidade=qualidade,
    )
    with open(INFO, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    print("\nPronto em %ds: %s focos de %s a %s, modo '%s', %d arquivo(s), %.0f MB (maior arquivo %.0f MB)."
          % (time.time() - t0, fmt(total), mn, mx, modo, len(arquivos), mb, maior))
    print("Agora: git add dados && git commit -m \"Atualiza dados\" && git push")


if __name__ == "__main__":
    main()
