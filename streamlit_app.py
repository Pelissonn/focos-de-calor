# -*- coding: utf-8 -*-
"""Focos de calor (NASA FIRMS) - dashboard cartografico em Streamlit.

Le os arquivos dados/focos/*.parquet (gerados por ferramentas/exportar_dados.py a partir do MySQL)
com DuckDB e desenha o mapa com pydeck e os graficos com Altair.
"""
import json
import math
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import pydeck as pdk
import streamlit as st

st.set_page_config(page_title="Focos de calor", page_icon="🔥", layout="wide")

RAIZ = Path(__file__).parent
DADOS = RAIZ / "dados"

# ---------------------------------------------------------------- aparencia
ESCURO = getattr(getattr(st.context, "theme", None), "type", "light") == "dark"
# intensidade: um matiz (laranja), claro -> escuro no tema claro e o inverso no escuro
RAMPA = (["#7d3917", "#9f4718", "#c25518", "#e26725", "#f6844e", "#fea77f", "#fecab4"] if ESCURO else
         ["#eea07d", "#e77e4c", "#d75e16", "#b84c05", "#943c04", "#722d02", "#531d00"])
AZUL = "#3987e5" if ESCURO else "#2a78d6"
CONF_COR = {"Baixa": "#184f95", "Nominal": "#3987e5", "Alta": "#86b6ef"} if ESCURO else \
           {"Baixa": "#86b6ef", "Nominal": "#3987e5", "Alta": "#1c5cab"}
CINZA = "#898781"
TINTA = "#f0efec" if ESCURO else "#0b0b0b"
HEX = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]


class DeckCompacto(pdk.Deck):
    """O pydeck gera o JSON com recuo e acentos escapados; compacto, o mapa chega ao navegador com menos da metade do tamanho."""
    def to_json(self):
        from pydeck.bindings.json_tools import default_serialize
        return json.dumps(self, sort_keys=True, default=default_serialize, separators=(",", ":"), ensure_ascii=False)

st.markdown("""
<style>
  .block-container { padding-top: 2rem; padding-bottom: 2rem; }
  [data-testid="stMetricValue"] { font-size: 1.55rem; }
  .legenda { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: .82rem; opacity: .9; margin-top: -.4rem; }
  .legenda .rampa { display: flex; }
  .legenda .rampa span { width: 34px; height: 10px; display: inline-block; }
  .legenda .ponta { font-variant-numeric: tabular-nums; }
  .legenda i { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 4px; }
  .nota { font-size: .8rem; opacity: .7; }
  .objetivo { font-size: .92rem; padding: .55rem .8rem; border-radius: 8px; background: rgba(128,128,128,.08);
              margin: -.4rem 0 .5rem 0; line-height: 1.5; }
  .manchete { border-left: 6px solid #888; padding: .7rem 1rem; margin: .4rem 0 1rem 0; border-radius: 6px;
              background: rgba(128,128,128,.06); line-height: 1.55; }
  .manchete .grande { font-size: 2.1rem; font-weight: 700; line-height: 1.15; margin-bottom: .2rem; }
  .manchete .grande span { font-size: 1rem; font-weight: 400; opacity: .75; }
  .manchete .nota { margin-top: .3rem; font-size: .85rem; }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- formatacao (pt-BR)
def fmt(n):
    return "{:,.0f}".format(n).replace(",", ".")


def fmt1(n):
    return "{:,.1f}".format(n).replace(",", "X").replace(".", ",").replace("X", ".")


def compacto(n):
    a = abs(n)
    for lim, suf in ((1e9, " bi"), (1e6, " mi"), (1e3, " mil")):
        if a >= lim:
            return fmt1(n / lim) + suf
    return fmt(n) if a >= 10 or float(n).is_integer() else fmt1(n)


def pct(a, b):
    return (fmt1(100 * a / b) + "%") if b else "—"


MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
data_br = lambda d: "%d %s %d" % (d.day, MESES[d.month - 1], d.year)
grau = lambda v, pos, neg, dig=2: ("{:.%df}" % dig).format(abs(v)).replace(".", ",") + "°" + (neg if v < 0 else pos)

SATS = {"Terra": "Terra", "T": "Terra", "Aqua": "Aqua", "A": "Aqua", "N": "Suomi NPP", "SNPP": "Suomi NPP",
        "N20": "NOAA-20", "J1": "NOAA-20", "N21": "NOAA-21", "J2": "NOAA-21"}
TIPOS = {-1: "Não informado (NRT)", 0: "Vegetação", 1: "Vulcão", 2: "Fonte estática", 3: "Offshore"}
ORD_CONF = ["Baixa", "Nominal", "Alta"]
CAIXA_CONT = {"AFR": [-19, -36, 52, 38], "AMS": [-82, -56, -34, 13], "AMC": [-93, 7, -59, 27], "AMN": [-168, 14, -52, 72],
              "ASI": [26, -11, 180, 78], "EUR": [-25, 34, 60, 72], "OCE": [110, -48, 180, 0], "ANT": [-180, -85, 180, -50]}


# ---------------------------------------------------------------- dados
@st.cache_resource(show_spinner=False)
def carrega():
    info_arq = DADOS / "info.json"
    if not info_arq.exists() or not list((DADOS / "focos").glob("*.parquet")):
        return None, None, None
    info = json.loads(info_arq.read_text(encoding="utf-8"))
    base = json.loads((DADOS / "mapa_base.json").read_text(encoding="utf-8"))
    db = duckdb.connect()
    db.execute("SET threads = 2")
    db.execute("SET memory_limit = '600MB'")
    db.execute("CREATE VIEW f AS SELECT * FROM read_parquet('%s')" % (DADOS / "focos" / "*.parquet").as_posix())
    cb = pd.DataFrame(info["combos"], columns=["combo", "sat", "inst", "conf", "per", "tipo", "classe"])
    db.register("cb_df", cb)
    db.execute("CREATE TABLE cb AS SELECT * FROM cb_df")
    return info, base, db


INFO, BASE, DB = carrega()
if INFO is None:
    st.error("Não encontrei os dados. Rode `python ferramentas/exportar_dados.py` no computador com o MySQL "
             "e envie a pasta `dados/` para o GitHub (veja o README).")
    st.stop()

FOCOS = INFO["modo"] == "focos"           # True: um registro por foco; False: grade de 0,1 grau por dia
Q, FS, FM = ("1", "frp", "frp") if FOCOS else ("qtd", "frp", "frp_max")
TEM_HORA = "minuto" in set(DB.execute("DESCRIBE f").df()["column_name"])   # horario da passagem (exportador novo)
PAISES = {p["c"]: p for p in BASE["paises"]}
REGIOES = BASE["regioes"]                  # indice + 1 = codigo do estado; [pais, nome, lon, lat, zoom, w, s, e, n]
CONT = BASE["continentes"]
COMBOS = pd.DataFrame(INFO["combos"], columns=["combo", "sat", "inst", "conf", "per", "tipo", "classe"])
D0, D1 = date.fromisoformat(INFO["intervalo"]["min"]), date.fromisoformat(INFO["intervalo"]["max"])
LACUNAS = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in INFO.get("lacunas", [])]


def nome_pais(c):
    c = c - 1000 if c >= 1000 else c
    return "Mar / sem país" if c == 0 else PAISES[c]["n"] if c in PAISES else "País %d" % c


def curto_pais(c):
    c = c - 1000 if c >= 1000 else c
    return PAISES[c]["r"] if c in PAISES else nome_pais(c)


def cont_de(c):
    if c >= 1000:
        return "ASI"
    return PAISES[c]["ct"] if c in PAISES else None


def nome_reg(r):
    return (REGIOES[r - 1][1] or nome_pais(REGIOES[r - 1][0])) if 0 < r <= len(REGIOES) else "Sem estado"


def q(sql, *args):
    return DB.execute(sql, list(args)).df()


# ---------------------------------------------------------------- estado (filtros)
VALORES = {
    "sat": [s for s in dict.fromkeys(COMBOS.sort_values("combo")["sat"])],
    "conf": sorted(COMBOS["conf"].unique(), key=lambda v: (ORD_CONF.index(v) if v in ORD_CONF else 9, v)),
    "per": sorted(COMBOS["per"].unique()),
    "tipo": sorted(COMBOS["tipo"].unique()),
}
_cl = q(f"SELECT c.classe, SUM({FS}) / SUM({Q}) AS m FROM f JOIN cb c USING (combo) GROUP BY 1")
VALORES["classe"] = list(_cl.assign(v=_cl["classe"] == "").sort_values(["v", "m"])["classe"])
MESES_LISTA = []
_m = date(D0.year, D0.month, 1)
while _m <= D1:
    MESES_LISTA.append(_m)
    _m = date(_m.year + (_m.month == 12), _m.month % 12 + 1, 1)
rot_mes = lambda m: "%s %d" % (MESES[m.month - 1], m.year)
fim_mes = lambda m: date(m.year + (m.month == 12), m.month % 12 + 1, 1) - timedelta(days=1)

PADRAO = dict(_periodo_ini=(MESES_LISTA[0], MESES_LISTA[-1]), sat=VALORES["sat"], conf=VALORES["conf"], per=VALORES["per"],
              tipo=VALORES["tipo"], classe=VALORES["classe"], cont="Todos", pais=-1, reg=0)   # -1 / 0 = todos
for k, v in PADRAO.items():
    st.session_state.setdefault(k, v)

# link compartilhavel: na primeira abertura os filtros podem vir da URL (?pais=76&reg=1316&de=2020-08&ate=2020-09)
if not st.session_state.get("_url_lida"):
    st.session_state["_url_lida"] = True
    qp = st.query_params
    try:
        mes = lambda t: date(int(t[:4]), int(t[5:7]), 1)
        de_ = max(mes(qp["de"]), MESES_LISTA[0]) if "de" in qp else MESES_LISTA[0]
        ate_ = min(mes(qp["ate"]), MESES_LISTA[-1]) if "ate" in qp else MESES_LISTA[-1]
        if de_ <= ate_:
            st.session_state._periodo_ini = (de_, ate_)
        if qp.get("cont") in CONT:
            st.session_state.cont = qp["cont"]
        if qp.get("pais") and int(qp["pais"]) in PAISES:
            st.session_state.pais = int(qp["pais"])
            st.session_state.reg = 0
            if qp.get("reg") and 0 < int(qp["reg"]) <= len(REGIOES):
                st.session_state.reg = int(qp["reg"])
    except (ValueError, KeyError):
        pass


def limpar():
    for k, v in PADRAO.items():
        st.session_state[k] = v
    st.session_state.pop("periodo", None)      # o controle de periodo volta ao valor inicial (todo o periodo)


# clique no mapa: escolhe o pais (antes de desenhar os filtros)
sel = st.session_state.get("mapa")
if sel and sel.get("selection", {}).get("objects"):
    objs = [o for lst in sel["selection"]["objects"].values() for o in lst]
    alvo = objs[0].get("pais") if objs else None
    marca = json.dumps(sel["selection"]["objects"], sort_keys=True, default=str)
    if alvo and marca != st.session_state.get("_ultimo_clique") and alvo != st.session_state.pais:
        st.session_state.pais, st.session_state.reg = int(alvo), 0
        st.session_state.cont = "Todos"
    st.session_state["_ultimo_clique"] = marca

# ---------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("Filtros")
    st.select_slider("Período (UTC)", options=MESES_LISTA, value=st.session_state._periodo_ini, key="periodo", format_func=rot_mes)
    st.multiselect("Satélite", VALORES["sat"], key="sat",
                   format_func=lambda s: SATS.get(s, s) + " (" + COMBOS.loc[COMBOS.sat == s, "inst"].iloc[0] + ")")
    st.pills("Confiança", VALORES["conf"], key="conf", selection_mode="multi")
    st.pills("Dia / noite", VALORES["per"], key="per", selection_mode="multi")
    st.pills("Tipo de fonte", VALORES["tipo"], key="tipo", selection_mode="multi", format_func=lambda t: TIPOS.get(t, "Tipo %s" % t))
    st.pills("Intensidade (classe de FRP)", VALORES["classe"], key="classe", selection_mode="multi",
             format_func=lambda c: c or "Sem FRP")
    st.divider()
    st.selectbox("Continente", ["Todos"] + list(CONT), key="cont", format_func=lambda c: "Todos" if c == "Todos" else CONT[c])
    ops = sorted((c for c in PAISES if st.session_state.cont == "Todos" or PAISES[c]["ct"] == st.session_state.cont
                  or (c == 643 and st.session_state.cont == "ASI")), key=lambda c: PAISES[c]["n"])
    if st.session_state.pais not in ops:
        st.session_state.pais = -1
    st.selectbox("País", [-1] + ops, key="pais", format_func=lambda c: "Todos" if c == -1 else PAISES[c]["n"])
    if st.session_state.pais != -1:
        regs = sorted((i + 1 for i, r in enumerate(REGIOES) if (r[0] % 1000 if r[0] >= 1000 else r[0]) == st.session_state.pais and r[1]),
                      key=lambda i: REGIOES[i - 1][1])
        if st.session_state.reg not in regs:
            st.session_state.reg = 0
        st.selectbox("Estado / província", [0] + regs, key="reg", format_func=lambda r: "Todos" if r == 0 else REGIOES[r - 1][1])
    st.divider()
    METRICAS = {"qtd": "Focos", "frp": "FRP total", "med": "FRP médio", "max": "FRP máximo"}
    st.radio("Cor do mapa", list(METRICAS), format_func=METRICAS.get, key="metrica", horizontal=True)
    if FOCOS:
        st.toggle("Mostrar cada foco quando forem poucos", value=True, key="pontos")
    st.button("Limpar filtros", on_click=limpar, width="stretch")

S = st.session_state
DE, ATE = S.periodo[0], fim_mes(S.periodo[1])
PAIS = None if S.pais == -1 else S.pais
REG = S.reg if (PAIS is not None and S.get("reg")) else None
_url = {}
if S.periodo[0] != MESES_LISTA[0]:
    _url["de"] = S.periodo[0].strftime("%Y-%m")
if S.periodo[1] != MESES_LISTA[-1]:
    _url["ate"] = S.periodo[1].strftime("%Y-%m")
if PAIS is not None:
    _url["pais"] = str(PAIS)
    if REG:
        _url["reg"] = str(REG)
elif S.cont != "Todos":
    _url["cont"] = S.cont
if dict(st.query_params) != _url:
    st.query_params.from_dict(_url)


# ---------------------------------------------------------------- filtro SQL
def combos_sel():
    m = (COMBOS.sat.isin(S.sat) & COMBOS.conf.isin(S.conf) & COMBOS.per.isin(S.per) &
         COMBOS.tipo.isin(S.tipo) & COMBOS.classe.isin(S.classe))
    return None if m.all() else [int(c) for c in COMBOS.loc[m, "combo"]]


def onde(com_periodo=True, com_regiao=True):
    w = ["TRUE"]
    if com_periodo and (DE > D0 or ATE < D1):
        w.append("dia BETWEEN DATE '%s' AND DATE '%s'" % (DE, ATE))
    c = combos_sel()
    if c is not None:
        w.append("combo IN (%s)" % (",".join(map(str, c)) or "-1"))
    if com_regiao:
        if REG:
            w.append("reg = %d" % REG)
        elif PAIS is not None:
            w.append("pais IN (%d, %d)" % (PAIS, PAIS + 1000))
        elif S.cont != "Todos":
            cods = [c for c in PAISES if PAISES[c]["ct"] == S.cont] + ([1643] if S.cont == "ASI" else [])
            if S.cont == "ASI":
                cods = [c for c in cods if c != 643] + [1643]
            w.append("pais IN (%s)" % ",".join(map(str, cods)))
    return " AND ".join(w)


W = onde()
nome_regiao = (nome_reg(REG) + ", " + nome_pais(PAIS)) if REG else nome_pais(PAIS) if PAIS is not None else \
    CONT[S.cont] if S.cont != "Todos" else "Mundo"


@st.cache_data(show_spinner=False, max_entries=200)
def consulta(sql):
    return q(sql)


# ---------------------------------------------------------------- topo: para quem, para que e a situacao agora
PUBLICO = "Gestores de defesa civil, órgãos ambientais e coordenadores de brigadas de incêndio"
PERGUNTA = "Onde os focos de calor estão acima do normal agora — e onde concentrar equipes de combate e fiscalização?"
st.title("Focos de calor · monitoramento para resposta a incêndios")
st.markdown('<div class="objetivo"><b>Para quem:</b> %s<br><b>Pergunta central:</b> %s</div>' % (PUBLICO, PERGUNTA),
            unsafe_allow_html=True)
st.caption("🛰️ NASA FIRMS (%s) · %s focos de %s a %s (UTC) · **última atualização: %s** · atualização manual, "
           "recomendada diária (os dados NRT saem cerca de 3 h após a passagem do satélite) · regras e qualidade na aba "
           "\"Qualidade dos dados\"" % (", ".join(SATS.get(s, s) for s in VALORES["sat"]), fmt(INFO["total"]),
                                         data_br(D0), data_br(D1), INFO["gerado_em"]))

k = consulta(f"""SELECT COALESCE(SUM({Q}),0) n, COALESCE(SUM({FS}),0) s, COALESCE(MAX({FM}),0) m,
    COALESCE(SUM(CASE WHEN c.classe <> '' THEN {Q} END),0) nf,
    COALESCE(SUM(CASE WHEN c.conf = 'Alta' THEN {Q} END),0) alta,
    COUNT(DISTINCT CASE WHEN pais > 0 THEN pais % 1000 END) np
    FROM f JOIN cb c USING (combo) WHERE {W}""").iloc[0]
TOTAL = int(k.n)
pico = consulta(f"SELECT dia, SUM({Q}) n FROM f WHERE {W} GROUP BY dia ORDER BY n DESC, dia LIMIT 1")

# Regra da situacao (a mesma em todo o app): focos dos ultimos 7 dias do periodo escolhido
# comparados com a media semanal de todo o periodo carregado, para a mesma regiao e filtros.
REF = min(ATE, D1)
R7 = (REF - timedelta(days=6), REF)
R7A = (REF - timedelta(days=13), REF - timedelta(days=7))
DIAS_DADOS = max(1, (D1 - D0).days + 1 - sum((b_ - a_).days + 1 for a_, b_ in LACUNAS))
W_SEM_PER = onde(com_periodo=False)
entre = lambda r: "dia BETWEEN DATE '%s' AND DATE '%s'" % r
sem7 = consulta(f"""SELECT COALESCE(SUM(CASE WHEN {entre(R7)} THEN {Q} END), 0) a,
    COALESCE(SUM(CASE WHEN {entre(R7A)} THEN {Q} END), 0) b, COALESCE(SUM({Q}), 0) t
    FROM f WHERE {W_SEM_PER}""").iloc[0]
N7, N7A = int(sem7.a), int(sem7.b)
MEDIA7 = float(sem7.t) / DIAS_DADOS * 7
RAZAO = N7 / MEDIA7 if MEDIA7 else 0.0
LIM_ALERTA, LIM_ATENCAO = 1.5, 1.0
SITUACAO = {"Alerta": ("🔴", "#d03b3b"), "Atenção": ("🟡", "#c98a00"), "Normal": ("🟢", "#2e8b57")}
ACAO = {"Alerta": "acionar brigadas e fiscalização nas áreas da aba 🚨 Alertas e avisar a defesa civil local.",
        "Atenção": "reforçar o monitoramento diário e deixar equipes de prontidão nas áreas com mais focos.",
        "Normal": "manter o monitoramento de rotina e investir em prevenção (aceiros, queima controlada, campanhas)."}
ACAO_AREA = {"Alerta": "Enviar brigadas e fiscalização; avisar a defesa civil local",
             "Atenção": "Equipes de prontidão; checar a área todos os dias",
             "Normal": "Rotina e prevenção"}


def situacao(r):
    return "Alerta" if r >= LIM_ALERTA else "Atenção" if r >= LIM_ATENCAO else "Normal"


SIT = situacao(RAZAO)
ICONE, COR_SIT = SITUACAO[SIT]

# manchete (storytelling com grandes numeros)
dif = (RAZAO - 1) * 100
comp = ("<b>%s%% acima</b> da média semanal" % fmt(dif)) if dif >= 0.5 else \
       ("<b>%s%% abaixo</b> da média semanal" % fmt(-dif)) if dif <= -0.5 else "<b>igual</b> à média semanal"
lider = consulta(f"SELECT {'reg' if PAIS is not None else 'pais % 1000'} AS c, SUM({Q}) n FROM f WHERE {W} "
                 f"GROUP BY 1 ORDER BY n DESC LIMIT 1")
historia = ""
if TOTAL and len(lider):
    c_ = int(lider.c[0])
    nome_l = nome_reg(c_) if PAIS is not None else (curto_pais(c_) if c_ else "o mar")
    historia = ("De %s a %s foram <b>%s focos</b>; %s concentrou <b>%s</b> deles e o pior dia foi %s (%s focos)."
                % (data_br(DE), data_br(ATE), fmt(TOTAL), nome_l, pct(int(lider.n[0]), TOTAL),
                   data_br(pico.dia[0]), fmt(pico.n[0])))
st.markdown(f"""<div class="manchete" style="border-left-color:{COR_SIT}">
  <div class="grande">{fmt(N7)} focos <span>nos últimos 7 dias · {nome_regiao}</span></div>
  <div>{data_br(R7[0])} a {data_br(R7[1])}: {comp} ({fmt(MEDIA7)} focos). {ICONE} <b style="color:{COR_SIT}">{SIT}</b> — {ACAO[SIT]}</div>
  <div class="nota">{historia}</div></div>""", unsafe_allow_html=True)

c1, c2, c3 = st.columns(3)
var7 = ("%+.0f" % (100 * (N7 - N7A) / N7A)) + "% vs. anterior" if N7A else None
c1.metric("Últimos 7 dias", fmt(N7), var7, delta_color="inverse", border=True,
          help="%s a %s · semana anterior: %s focos. Vermelho = mais focos que na semana anterior."
               % (data_br(R7[0]), data_br(R7[1]), fmt(N7A)))
c2.metric("Situação", "%s %s" % (ICONE, SIT), ("%s× a média" % fmt1(RAZAO)) if MEDIA7 else None, delta_color="off", delta_arrow="off",
          border=True, help="Alerta: 7 dias ≥ %s× a média semanal · Atenção: entre 1× e %s× · Normal: abaixo da média"
                            % (fmt1(LIM_ALERTA), fmt1(LIM_ALERTA)))
c3.metric("Focos no período", fmt(TOTAL), border=True,
          help="%s a %s · %s · FRP total %s MW · %s países" % (data_br(DE), data_br(ATE), nome_regiao, compacto(k.s), fmt(k.np)))
c4, c5, c6 = st.columns(3)
c4.metric("Pior dia", fmt(pico.n[0]) if len(pico) else "—", data_br(pico.dia[0]) if len(pico) else None,
          delta_color="off", delta_arrow="off", border=True, help="Dia com mais focos no período escolhido")
c5.metric("Intensidade média (FRP)", (fmt1(k.s / k.nf) + " MW") if k.nf else "—", border=True,
          help="Potência radiativa média por foco · maior foco: %s MW" % fmt1(k.m))
c6.metric("Alta confiança", pct(k.alta, TOTAL), border=True,
          help="Focos que o satélite classifica com confiança alta (%s). Baixa confiança pode ser reflexo de sol ou "
               "superfície quente." % fmt(k.alta))

if not TOTAL:
    st.info("Nenhum foco para os filtros escolhidos.")
    st.stop()

# ---------------------------------------------------------------- graficos
LOC = {"number": {"decimal": ",", "thousands": ".", "grouping": [3], "currency": ["R$", ""]},
       "time": {"dateTime": "%A, %e de %B de %Y. %X", "date": "%d/%m/%Y", "time": "%H:%M:%S", "periods": ["AM", "PM"],
                "days": ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"],
                "shortDays": ["dom", "seg", "ter", "qua", "qui", "sex", "sáb"],
                "months": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
                           "outubro", "novembro", "dezembro"], "shortMonths": MESES}}


def pronto(ch):
    return ch.configure(locale=LOC).configure_view(stroke=None)


def barras(df, nome_col, titulo_eixo="", cores=None, altura_linha=26):
    df = df.copy()
    df["pct"] = df["n"] / TOTAL
    df["n_txt"] = df["n"].map(fmt) + "  " + df["pct"].map(lambda x: fmt1(100 * x) + "%")
    df["frp_txt"] = df["s"].map(compacto) + " MW"
    enc_cor = alt.Color("cor:N", scale=None) if cores is not None else alt.value(AZUL)
    if cores is not None:
        df["cor"] = cores
    ordem = list(df[nome_col])
    base = alt.Chart(df).encode(y=alt.Y(nome_col + ":N", sort=ordem, title=None, axis=alt.Axis(labelLimit=170, labelOverlap=False, ticks=False, domain=False)))
    b = base.mark_bar(cornerRadiusEnd=4, height=14).encode(
        x=alt.X("n:Q", title=titulo_eixo, axis=None), color=enc_cor,
        tooltip=[alt.Tooltip(nome_col + ":N", title=""), alt.Tooltip("n:Q", title="Focos", format=",.0f"),
                 alt.Tooltip("pct:Q", title="% do total", format=".1%"), alt.Tooltip("frp_txt:N", title="FRP total")])
    t = base.mark_text(align="left", dx=6, fontSize=12).encode(x="n:Q", text="n_txt:N", color=alt.value(CINZA))
    return pronto((b + t).properties(height=max(60, altura_linha * len(df))))


# ---------------------------------------------------------------- meio: tendencia
st.subheader("Evolução no tempo")
st.caption("%s · os picos mostram quando reforçar equipes; compare com a linha da média" % nome_regiao)
dias_periodo = (ATE - DE).days
gran = st.radio("Agrupar por", ["Dia", "Mês", "Ano"], index=0 if dias_periodo <= 400 else 1, horizontal=True, key="gran")
trunc = {"Dia": "day", "Mês": "month", "Ano": "year"}[gran]
se = consulta(f"SELECT DATE_TRUNC('{trunc}', dia) AS t, SUM({Q}) n, SUM({FS}) s FROM f WHERE {W} GROUP BY 1 ORDER BY 1")
se["t"] = pd.to_datetime(se["t"])
# completa os periodos sem focos com zero (e deixa as lacunas de dados sem linha)
todos = pd.date_range(pd.Timestamp(max(DE, D0)).to_period({"day": "D", "month": "M", "year": "Y"}[trunc]).start_time,
                      pd.Timestamp(min(ATE, D1)), freq={"day": "D", "month": "MS", "year": "YS"}[trunc])
se = se.set_index("t").reindex(todos, fill_value=0).rename_axis("t").reset_index()
for a_, b_ in LACUNAS:
    fim_t = se["t"] + {"day": pd.Timedelta(days=0), "month": pd.offsets.MonthEnd(0), "year": pd.offsets.YearEnd(0)}[trunc]
    se.loc[(se["t"] >= pd.Timestamp(a_)) & (fim_t <= pd.Timestamp(b_)), ["n", "s"]] = None
se["s_txt"] = se["s"].map(lambda x: compacto(x) + " MW" if pd.notna(x) else "sem dados")
fmt_t = {"day": "%d %b %Y", "month": "%b %Y", "year": "%Y"}[trunc]
base = alt.Chart(se).encode(x=alt.X("t:T", title=None, axis=alt.Axis(format="%Y" if dias_periodo > 900 else "%b %Y")))
area = base.mark_area(color=AZUL, opacity=0.12).encode(y=alt.Y("n:Q", title="Focos", axis=alt.Axis(format="~s" if se.n.max() >= 1e4 else ",.0f")))
linha = base.mark_line(color=AZUL, strokeWidth=2).encode(y="n:Q")
regua = alt.selection_point(nearest=True, on="pointerover", fields=["t"], empty=False)
pontos_t = base.mark_point(size=60, filled=True, color=AZUL).encode(
    y="n:Q", opacity=alt.condition(regua, alt.value(1), alt.value(0)),
    tooltip=[alt.Tooltip("t:T", title="Período", format=fmt_t), alt.Tooltip("n:Q", title="Focos", format=",.0f"),
             alt.Tooltip("s_txt:N", title="FRP total")]).add_params(regua)
regra = base.mark_rule(color=CINZA).encode(opacity=alt.condition(regua, alt.value(0.6), alt.value(0)))
camadas_t = [area, linha, regra, pontos_t]
if trunc == "day" and MEDIA7:
    camadas_t.append(alt.Chart(pd.DataFrame({"m": [MEDIA7 / 7]})).mark_rule(color=CINZA, strokeDash=[5, 4]).encode(y="m:Q"))
lac = [dict(a=pd.Timestamp(max(a_, DE)), b=pd.Timestamp(min(b_, ATE))) for a_, b_ in LACUNAS if b_ >= DE and a_ <= ATE]
if lac:
    camadas_t.insert(0, alt.Chart(pd.DataFrame(lac)).mark_rect(color=CINZA, opacity=0.15).encode(x="a:T", x2="b:T"))
st.altair_chart(pronto(alt.layer(*camadas_t).properties(height=320)), width="stretch")
if len(se.dropna()):
    top = se.dropna().sort_values("n", ascending=False).iloc[0]
    st.caption("Pico: %s focos em %s%s" % (fmt(top.n), top.t.strftime({"day": "%d/%m/%Y", "month": "%m/%Y", "year": "%Y"}[trunc]),
                                          (" · faixas cinzas = períodos sem dados carregados" if lac else "") +
                                          (" · linha tracejada = média diária de todo o período (o normal)" if trunc == "day" else "")))


# ---------------------------------------------------------------- mapa
def caixa_regiao():
    if REG:
        r = REGIOES[REG - 1]
        return r[5:9]
    if PAIS is not None:
        return PAISES[PAIS]["bb"]
    if S.cont != "Todos":
        return CAIXA_CONT[S.cont]
    return None


def vista(bb, alt_px=560, larg_px=1100):
    if bb is None:
        return pdk.ViewState(longitude=10, latitude=18, zoom=0.9 if larg_px > 900 else 0.3)
    w, s, e, n = bb
    my = lambda la: math.log(math.tan(math.pi / 4 + max(-85, min(85, la)) * math.pi / 360))
    zx = math.log2(larg_px * 360 / 512 / max(e - w, 1e-3))
    zy = math.log2(alt_px * 2 * math.pi / 512 / max(my(n) - my(s), 1e-3))
    return pdk.ViewState(longitude=(w + e) / 2, latitude=math.degrees(math.atan(math.sinh((my(n) + my(s)) / 2))),
                         zoom=max(0.3, min(zx, zy) - 0.15))


METRICA = S.metrica
EXPR = {"qtd": "n", "frp": "s", "med": "s / n", "max": "m"}[METRICA]
UN = "" if METRICA == "qtd" else " MW"
bb = caixa_regiao()
camadas, legenda_html = [], ""

# focos individuais quando forem poucos (so no modo 'focos')
pontos = None
if FOCOS and S.get("pontos") and (PAIS is not None or REG) and TOTAL <= 15000:
    pontos = consulta(f"""SELECT ROUND(lat / 1e5, 4) AS lat, ROUND(lon / 1e5, 4) AS lon, dia, frp, c.sat, c.inst, c.conf, c.per, c.classe, reg
        FROM f JOIN cb c USING (combo) WHERE {W}""")

if pontos is not None:
    cls = [v for v in VALORES["classe"] if v]
    passo = {v: min(6, round(i * 6 / max(1, len(cls) - 1))) for i, v in enumerate(cls)}
    cor = pontos["classe"].map(lambda v: HEX(RAMPA[passo[v]]) if v in passo else HEX(CINZA))
    pontos = pontos.assign(
        k=cor,
        t=pontos["reg"].map(nome_reg),
        a=pontos["frp"].map(lambda v: fmt1(v) + " MW") + " · " + pontos["classe"].replace("", "sem FRP").str.lower(),
        b=pontos["dia"].map(lambda d: data_br(d)) + " · " + pontos["sat"].map(lambda s: SATS.get(s, s)) + " · conf. " + pontos["conf"].str.lower(),
        c=pontos["per"] + " · " + pontos["lat"].map(lambda v: grau(v, "N", "S", 3)) + " " + pontos["lon"].map(lambda v: grau(v, "L", "O", 3)),
        rd=pontos["inst"].map(lambda i: 500 if i == "MODIS" else 190),
    )[["lat", "lon", "k", "t", "a", "b", "c", "rd"]]
    camadas.append(pdk.Layer("ScatterplotLayer", pontos, id="focos", get_position="[lon, lat]", get_radius="rd",
                             radius_min_pixels=2.5, radius_max_pixels=40, get_fill_color="k", opacity=0.92,
                             stroked=True, get_line_color=[255, 255, 255, 120] if not ESCURO else [0, 0, 0, 120],
                             line_width_min_pixels=0.5, pickable=True, auto_highlight=True))
    leg = "".join('<span><i style="background:%s"></i>%s</span>' % (RAMPA[passo[v]], v) for v in cls)
    legenda_html = '<div class="legenda"><b>Cada ponto é um foco · cor pela intensidade (FRP):</b> %s</div>' % leg
    resol_txt = "%s focos individuais" % fmt(len(pontos))
else:
    # celulas: escolhe a mais fina que der ate ~40 mil celulas
    # o Streamlit reenvia o mapa a cada interacao: ate ~25 mil celulas mantem o app leve
    opcoes = [0.5, 0.25, 0.1, 0.05] if FOCOS else [0.5, 0.25, 0.1]
    if bb is None:
        opcoes = [1.0, 0.5]
    elif PAIS is None and not REG:
        opcoes = [1.0, 0.5, 0.25]
    cel = None
    for res in reversed(opcoes):
        div = round(1e5 * res)
        cel = consulta(f"""SELECT CAST(FLOOR(lat / {div}) AS INTEGER) la, CAST(FLOOR(lon / {div}) AS INTEGER) lo,
            SUM({Q}) n, SUM({FS}) s, MAX({FM}) m, MODE(pais) pais, MODE(reg) reg FROM f WHERE {W} GROUP BY 1, 2""")
        if len(cel) <= 25000 or res == opcoes[0]:
            break
    v = cel.eval(EXPR) if METRICA != "qtd" else cel["n"]
    pos = v[v > 0]
    lo_, hi_ = (1.0, float(v.max())) if METRICA == "qtd" else (float(pos.quantile(0.02)), float(pos.quantile(0.98)))
    hi_ = max(hi_, lo_ * 10 if lo_ > 0 else 1)
    lo_ = max(lo_, hi_ / 1e5)
    k_ = ((v.clip(lower=lo_).apply(math.log) - math.log(lo_)) / (math.log(hi_) - math.log(lo_)) * 7).clip(0, 6).astype(int)
    cores = k_.map(lambda i: HEX(RAMPA[i]))
    dig = 0 if res >= 1 else 2
    cel = cel.assign(
        x=(cel.lo * res).round(2), y=(cel.la * res).round(2), k=cores,
        t=[(nome_reg(r) + ", " + curto_pais(p)) if (r and PAIS is not None) else (curto_pais(p) if p else "Mar / sem país")
                for p, r in zip(cel.pais, cel.reg)],
        a=cel.n.map(fmt) + " focos · " + cel.s.map(compacto) + " MW",
        b="médio " + (cel.s / cel.n).map(fmt1) + " · máx. " + cel.m.map(fmt1) + " MW",
        c=(cel.la * res + res / 2).map(lambda x: grau(x, "N", "S", dig)) + " " + (cel.lo * res + res / 2).map(lambda x: grau(x, "L", "O", dig)),
        pais=cel.pais.astype(int) % 1000,
    )[["x", "y", "k", "t", "a", "b", "c", "pais"]]
    camadas.append(pdk.Layer("PolygonLayer", cel, id="celulas",
                             get_polygon="[[x, y], [x + %g, y], [x + %g, y + %g], [x, y + %g]]" % (res, res, res, res),
                             get_fill_color="k", opacity=0.88, stroked=False, pickable=True, auto_highlight=True,
                             highlight_color=[255, 255, 255, 90] if ESCURO else [0, 0, 0, 70]))
    rot = (lambda x: compacto(x)) if METRICA in ("qtd", "frp") else (lambda x: fmt1(x))
    bordas = [lo_ * (hi_ / lo_) ** (i / 7) for i in range(8)]
    marcas = "".join('<span style="background:%s"></span>' % c for c in RAMPA)
    legenda_html = ('<div class="legenda"><b>%s por célula de %s°</b> <span class="ponta">%s</span><span class="rampa">%s</span>'
                    '<span class="ponta">%s%s</span> <span class="nota">(escala logarítmica)</span></div>'
                    % (METRICAS[METRICA], ("%g" % res).replace(".", ","), rot(bordas[0]), marcas, rot(bordas[-1]), UN))
    resol_txt = "células de %s° (≈ %s km)" % (("%g" % res).replace(".", ","), fmt(111 * res))

# contornos: pais selecionado e divisas de estados dele
if PAIS is not None and PAIS in PAISES:
    p = PAISES[PAIS]
    camadas.append(pdk.Layer("PolygonLayer", [{"poly": pol} for pol in p["poly"]], id="contorno", get_polygon="poly",
                             filled=False, stroked=True, get_line_color=HEX(TINTA) + [200], line_width_min_pixels=1.6))
    w, s_, e, n = p["bb"]
    linhas = [{"p": l["p"]} for l in BASE["linhas_estados"]
              if l["bb"][2] >= w and l["bb"][0] <= e and l["bb"][3] >= s_ and l["bb"][1] <= n]
    camadas.append(pdk.Layer("PathLayer", linhas, id="estados", get_path="p", get_color=HEX(TINTA) + [90],
                             width_min_pixels=0.8))

dica = {"html": "<b>{t}</b><br/>{a}<br/>{b}<br/><span style='opacity:.75'>{c}</span>",
        "style": {"fontSize": "12px", "backgroundColor": "#1a1a19" if ESCURO else "#fcfcfb",
                  "color": TINTA, "border": "1px solid rgba(128,128,128,.3)", "borderRadius": "8px"}}

st.subheader("Onde estão os focos")
st.caption("%s · %s · passe o mouse para ver os valores · clique numa célula para filtrar pelo país" % (nome_regiao, resol_txt))
st.pydeck_chart(DeckCompacto(layers=camadas, initial_view_state=vista(bb), tooltip=dica, map_style=None),
                height=560, on_select="rerun", selection_mode="single-object", key="mapa")
st.markdown(legenda_html, unsafe_allow_html=True)
st.caption("Fundo: © CARTO © OpenStreetMap. Cada foco é atribuído ao país e ao estado pela célula de 0,1° (≈ 11 km) em que cai; "
           "focos no mar a mais de ~30 km da costa ficam como \"sem país\".")


aba = st.tabs(["🚨 Alertas", "Ranking", "Calendário", "Horário", "Categorias", "Qualidade dos dados", "Dados"])

with aba[0]:
    st.markdown("**Onde agir primeiro** · estados e províncias com mais focos nos últimos 7 dias (%s a %s) do que a "
                "média semanal deles · %s" % (data_br(R7[0]), data_br(R7[1]), nome_regiao))
    al = consulta(f"""SELECT reg, MODE(pais) pais,
        COALESCE(SUM(CASE WHEN {entre(R7)} THEN {Q} END), 0) n7,
        COALESCE(SUM(CASE WHEN {entre(R7A)} THEN {Q} END), 0) n7a,
        COALESCE(SUM(CASE WHEN {entre(R7)} THEN {FS} END), 0) s7,
        COALESCE(MAX(CASE WHEN {entre(R7)} THEN {FM} END), 0) m7,
        SUM({Q}) t FROM f WHERE {W_SEM_PER} AND reg > 0 GROUP BY reg""")
    fa, fb = st.columns([1, 2])
    minimo = fa.number_input("Mínimo de focos na semana", 1, 10000, 20, step=10,
                             help="Evita alertas por poucos focos isolados")
    ver = fb.pills("Mostrar", list(SITUACAO), default=["Alerta", "Atenção"], selection_mode="multi", key="ver_sit")
    al["media7"] = al.t / DIAS_DADOS * 7
    al["razao"] = al.n7 / al.media7
    al = al[al.n7 >= minimo].copy()
    al["sit"] = al.razao.map(situacao)
    na, nt = int((al.sit == "Alerta").sum()), int((al.sit == "Atenção").sum())
    m1, m2, m3 = st.columns(3)
    m1.metric("🔴 Áreas em alerta", fmt(na), border=True, help="7 dias ≥ %s× a média semanal" % fmt1(LIM_ALERTA))
    m2.metric("🟡 Áreas em atenção", fmt(nt), border=True, help="7 dias entre 1× e %s× a média semanal" % fmt1(LIM_ALERTA))
    m3.metric("Focos nas áreas em alerta", fmt(al.loc[al.sit == "Alerta", "n7"].sum()), border=True,
              help="Soma dos focos dos últimos 7 dias nas áreas em alerta")
    al = al[al.sit.isin(ver or [])]
    al["excesso"] = al.n7 - al.media7
    al = al.sort_values(["excesso", "n7"], ascending=False)
    tab_al = pd.DataFrame({
        "Situação": al.sit.map(lambda s: SITUACAO[s][0] + " " + s),
        "Estado / província": al.reg.map(lambda r: nome_reg(int(r))),
        "País": al.pais.map(lambda c: curto_pais(int(c))),
        "Focos 7 dias": al.n7.astype(int),
        "Média semanal": al.media7.round(0).astype(int),
        "x média": al.razao.round(1),
        "vs. semana anterior": [((a_ - b_) / b_) if b_ else None for a_, b_ in zip(al.n7, al.n7a)],
        "FRP 7 dias (MW)": al.s7.round(0),
        "Maior foco (MW)": al.m7.round(1),
        "Ação sugerida": al.sit.map(ACAO_AREA),
    })
    if tab_al.empty:
        st.success("Nenhuma área nas situações escolhidas com pelo menos %s focos na semana." % fmt(minimo))
    else:
        st.dataframe(tab_al, width="stretch", hide_index=True, height=min(520, 38 + 35 * len(tab_al)),
                     column_config={
                         "Focos 7 dias": st.column_config.NumberColumn(format="localized"),
                         "Média semanal": st.column_config.NumberColumn(format="localized"),
                         "x média": st.column_config.ProgressColumn("× a média", min_value=0, max_value=max(3.0, float(tab_al["x média"].max())),
                                                                    format="%.1f×"),
                         "vs. semana anterior": st.column_config.NumberColumn(format="percent"),
                         "FRP 7 dias (MW)": st.column_config.NumberColumn(format="localized"),
                         "Maior foco (MW)": st.column_config.NumberColumn(format="localized"),
                         "Ação sugerida": st.column_config.TextColumn(width="large")})
        st.download_button("Baixar lista de áreas (%s) em CSV" % fmt(len(tab_al)),
                           tab_al.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                           file_name="areas_em_alerta_%s.csv" % REF.isoformat(), mime="text/csv")
    st.caption("Ordenado pelo excesso de focos em relação ao normal (focos dos 7 dias − média semanal). "
               "Mude a data final do período na barra lateral para ver como estava a situação em outra semana. "
               "Com só um ano de dados, o \"normal\" é a média do ano todo; com mais anos, o ideal é comparar com a "
               "mesma época dos anos anteriores.")

with aba[1]:
    if PAIS is not None:
        rk = consulta(f"SELECT reg, SUM({Q}) n, SUM({FS}) s FROM f WHERE {W} GROUP BY 1 ORDER BY n DESC LIMIT 30")
        rk["nome"] = rk.reg.map(nome_reg)
        st.markdown("**Estados e províncias com mais focos** · %s" % nome_pais(PAIS))
    else:
        rk = consulta(f"SELECT pais % 1000 AS p, SUM({Q}) n, SUM({FS}) s FROM f WHERE {W} GROUP BY 1 ORDER BY n DESC LIMIT 25")
        rk["nome"] = rk.p.map(lambda c: curto_pais(int(c)) if c else "Mar / sem país")
        st.markdown("**Países com mais focos** · %s · escolha um país na barra lateral (ou clique no mapa) para ver os estados" % nome_regiao)
    st.altair_chart(barras(rk[["nome", "n", "s"]], "nome"), width="stretch")
    if PAIS is None and S.cont == "Todos":
        ct = consulta(f"SELECT pais, SUM({Q}) n, SUM({FS}) s FROM f WHERE {W} GROUP BY 1")
        ct["cont"] = ct.pais.map(lambda c: CONT.get(cont_de(int(c)), "Mar / sem país") if c else "Mar / sem país")
        ct = ct.groupby("cont", as_index=False)[["n", "s"]].sum().sort_values("n", ascending=False)
        st.markdown("**Continentes** · a Rússia é dividida entre Europa e Ásia")
        st.altair_chart(barras(ct, "cont"), width="stretch")
with aba[2]:
    st.markdown("**Focos por mês** · todo o período dos dados, com os demais filtros aplicados")
    cal = consulta(f"SELECT YEAR(dia) ano, MONTH(dia) mes, SUM({Q}) n, SUM({FS}) s FROM f WHERE {onde(com_periodo=False)} GROUP BY 1, 2")
    grade = pd.DataFrame([(a_, m_) for a_ in range(D0.year, D1.year + 1) for m_ in range(1, 13)], columns=["ano", "mes"])
    cal = grade.merge(cal, how="left", on=["ano", "mes"])
    ini = cal.apply(lambda r: date(int(r.ano), int(r.mes), 1), axis=1)
    sem = ini.map(lambda d: fim_mes(d) < D0 or d > D1 or any(a_ <= max(d, D0) and min(fim_mes(d), D1) <= b_ for a_, b_ in LACUNAS))
    cal["situ"] = ["sem dados" if s_ else ("nenhum foco" if pd.isna(n_) else "") for s_, n_ in zip(sem, cal.n)]
    cal["n"] = cal["n"].fillna(0)
    cal["mes_txt"] = cal.mes.map(lambda m_: MESES[m_ - 1])
    cal["rot"] = cal.mes_txt + " " + cal.ano.astype(str)
    cal["info"] = [s_ or (fmt(n_) + " focos") for s_, n_ in zip(cal.situ, cal.n)]
    enc = dict(x=alt.X("ano:O", title=None, axis=alt.Axis(labelAngle=0, ticks=False, domain=False)),
               y=alt.Y("mes_txt:N", sort=MESES, title=None, axis=alt.Axis(labelOverlap=False, ticks=False, domain=False)))
    dica_cal = [alt.Tooltip("rot:N", title="Mês"), alt.Tooltip("info:N", title="")]
    vazio = alt.Chart(cal[cal.n == 0]).mark_rect(cornerRadius=3, strokeWidth=1, stroke="#c3c2b7" if not ESCURO else "#383835",
        fill="#e1e0d9" if not ESCURO else "#2c2c2a", fillOpacity=alt.ExprRef("datum.situ == 'sem dados' ? 1 : 0")).encode(**enc, tooltip=dica_cal)
    cor = alt.Chart(cal[cal.n > 0]).mark_rect(cornerRadius=3).encode(
        **enc, color=alt.Color("n:Q", title="Focos no mês", scale=alt.Scale(range=RAMPA),
                               legend=alt.Legend(format=",.0f", orient="bottom", gradientLength=260)),
        tooltip=dica_cal)
    st.altair_chart(pronto((vazio + cor).properties(height=380)), width="stretch")
    st.caption("Contorno vazio = nenhum foco no mês · cinza = sem dados carregados naquele mês")

with aba[3]:
    if not TEM_HORA:
        st.info("Os dados exportados não trazem o horário da passagem. Rode de novo `python ferramentas/exportar_dados.py` "
                "(no modo de grade, usado quando os dados são grandes demais, o horário não é exportado).")
    else:
        # hora solar local aproximada: hora UTC + longitude / 15 (cada 15° = 1 hora)
        HS = "CAST(FLOOR(((minuto / 60.0 + lon / 1e5 / 15) % 24 + 24) % 24) AS INTEGER)"
        ho = consulta(f"""SELECT {HS} h, c.per, SUM({Q}) n, SUM({FS}) s FROM f JOIN cb c USING (combo)
            WHERE {W} GROUP BY ALL""")
        st.markdown("**Focos por hora solar local** · a hora do dia no lugar do foco (hora UTC + longitude ÷ 15)")
        ho["s_txt"] = ho.s.map(lambda x: compacto(x) + " MW")
        ho["hora"] = ho.h.map(lambda h: "%02dh" % h)
        cores_per = {"Dia": "#e26725" if ESCURO else "#d75e16", "Noite": AZUL}
        st.altair_chart(pronto(alt.Chart(ho).mark_bar(cornerRadiusEnd=3).encode(
            x=alt.X("h:O", title="Hora solar local", axis=alt.Axis(labelAngle=0, labelExpr="datum.value + 'h'")),
            y=alt.Y("n:Q", title="Focos", stack=True, axis=alt.Axis(format="~s")),
            color=alt.Color("per:N", title=None, scale=alt.Scale(domain=list(cores_per), range=list(cores_per.values())),
                            legend=alt.Legend(orient="top")),
            tooltip=[alt.Tooltip("hora:N", title="Hora"), alt.Tooltip("per:N", title="Período"),
                     alt.Tooltip("n:Q", title="Focos", format=",.0f"), alt.Tooltip("s_txt:N", title="FRP total")],
        ).properties(height=280)), width="stretch")
        tot_h = ho.groupby("h")[["n", "s"]].sum()
        if len(tot_h):
            fr_med = (tot_h.s / tot_h.n).sort_values(ascending=False)
            st.caption("Hora com mais focos: %02dh (%s focos) · maior FRP médio por foco: %02dh (%s MW). "
                       "Satélites em órbita polar passam sempre perto do mesmo horário local — por isso os focos se "
                       "concentram em poucas horas (VIIRS ~01h30 e ~13h30; MODIS ~10h30, 13h30, 22h30 e 01h30)."
                       % (tot_h.n.idxmax(), fmt(tot_h.n.max()), fr_med.index[0], fmt1(fr_med.iloc[0])))

        st.markdown("**Hora × mês** · como o horário dos focos muda ao longo do período")
        hm = consulta(f"SELECT DATE_TRUNC('month', dia) m, {HS} h, SUM({Q}) n FROM f WHERE {W} GROUP BY ALL")
        hm["m"] = pd.to_datetime(hm["m"])
        hm["mes"] = hm.m.map(lambda d: "%s %d" % (MESES[d.month - 1], d.year))
        ordem_m = list(hm.sort_values("m")["mes"].unique())
        st.altair_chart(pronto(alt.Chart(hm).mark_rect(cornerRadius=2).encode(
            x=alt.X("h:O", title="Hora solar local", axis=alt.Axis(labelAngle=0, labelExpr="datum.value + 'h'")),
            y=alt.Y("mes:N", sort=ordem_m, title=None, axis=alt.Axis(ticks=False, domain=False, labelOverlap=False)),
            color=alt.Color("n:Q", title="Focos", scale=alt.Scale(type="log", range=RAMPA),
                            legend=alt.Legend(format="~s", orient="bottom", gradientLength=260)),
            tooltip=[alt.Tooltip("mes:N", title="Mês"), alt.Tooltip("h:O", title="Hora"),
                     alt.Tooltip("n:Q", title="Focos", format=",.0f")],
        ).properties(height=max(120, 22 * len(ordem_m)))), width="stretch")

        st.markdown("**Dia da semana** · focos ligados a queimadas agrícolas costumam variar com a semana de trabalho")
        sem = consulta(f"SELECT ISODOW(dia) d, SUM({Q}) n, SUM({FS}) s FROM f WHERE {W} GROUP BY 1 ORDER BY 1")
        nomes_d = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
        sem["nome"] = sem.d.map(lambda d: nomes_d[int(d) - 1])
        st.altair_chart(barras(sem[["nome", "n", "s"]], "nome"), width="stretch")
        st.caption("Horas calculadas pela longitude de cada foco; o dia da semana usa a data em UTC.")

with aba[4]:
    cat = consulta(f"""SELECT c.sat, c.inst, c.conf, c.per, c.tipo, c.classe, SUM({Q}) n, SUM({FS}) s
        FROM f JOIN cb c USING (combo) WHERE {W} GROUP BY ALL""")

    def por(col, ordem=None):
        d = cat.groupby(col, as_index=False)[["n", "s"]].sum()
        if ordem is not None:
            d[col] = pd.Categorical(d[col], ordem, ordered=True)
            d = d.sort_values(col)
        else:
            d = d.sort_values("n", ascending=False)
        return d

    a, b = st.columns(2)
    with a:
        st.markdown("**Intensidade** · classe de potência radiativa (FRP)")
        d = por("classe", VALORES["classe"])
        reais = [v for v in VALORES["classe"] if v]
        cores = [RAMPA[min(6, round(reais.index(v) * 6 / max(1, len(reais) - 1)))] if v in reais else CINZA for v in d.classe]
        d["nome"] = d.classe.astype(str).replace("", "Sem FRP")
        st.altair_chart(barras(d[["nome", "n", "s"]], "nome", cores=cores), width="stretch")
        st.markdown("**Nível de confiança**")
        d = por("conf", VALORES["conf"])
        d["nome"] = d.conf.astype(str)
        st.altair_chart(barras(d[["nome", "n", "s"]], "nome", cores=[CONF_COR.get(v, AZUL) for v in d.nome]), width="stretch")
        st.markdown("**Dia e noite** · horário da passagem do satélite")
        d = por("per")
        d["nome"] = d.per
        st.altair_chart(barras(d[["nome", "n", "s"]], "nome"), width="stretch")
    with b:
        st.markdown("**Satélite e sensor** · o VIIRS enxerga focos menores que o MODIS")
        d = cat.groupby(["sat", "inst"], as_index=False)[["n", "s"]].sum()
        d["o"] = d.sat.map(lambda s_: VALORES["sat"].index(s_))
        d = d.sort_values("o")
        d["nome"] = d.sat.map(lambda s_: SATS.get(s_, s_)) + " (" + d.inst + ")"
        st.altair_chart(barras(d[["nome", "n", "s"]], "nome"), width="stretch")
        st.markdown("**Tipo de fonte** · origem do sinal térmico")
        d = por("tipo")
        d["nome"] = d.tipo.map(lambda t: TIPOS.get(int(t), "Tipo %s" % t))
        st.altair_chart(barras(d[["nome", "n", "s"]], "nome"), width="stretch")

with aba[5]:
    ql = INFO.get("qualidade") or {}
    st.markdown("**De onde vêm os dados e quanto confiar neles**")
    if ql:
        orig, res = ql["linhas_origem"], ql["linhas_resumo"]
        dup = ql.get("duplicados")
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("Linhas no MySQL", fmt(orig), border=True, help="Tabela %s do banco %s" % (INFO["tabela"], INFO["banco"]))
        q2.metric("Linhas no dashboard", fmt(res), "100% conferido" if res == orig else "%s a menos" % fmt(orig - res),
                  delta_color="normal" if res == orig else "inverse", border=True,
                  help="Todas as linhas do MySQL chegam ao dashboard; a carga confere a contagem de cada bloco")
        q3.metric("Duplicados", fmt(dup) if dup is not None else "—", border=True,
                  help="Focos com mesmo instante, coordenada (0,00001°), satélite e categorias")
        sem_pais = int(q(f"SELECT COALESCE(SUM({Q}), 0) n FROM f WHERE pais = 0").n[0])
        q4.metric("Focos sem país (mar)", pct(sem_pais, res), border=True,
                  help="%s focos a mais de ~30 km da costa (plataformas de petróleo, navios)" % fmt(sem_pais))
        ok = lambda v: "✅ OK" if not v else "⚠️ Atenção"
        pq = lambda v: "< 0,01%" if 0 < v < orig * 1e-4 else pct(v, orig)
        chk = pd.DataFrame([
            ("Coordenada ausente ou fora da faixa", ql["sem_coordenada"] + ql["coordenada_invalida"],
             ok(ql["sem_coordenada"] + ql["coordenada_invalida"]), "Nenhum caso"),
            ("Data/hora ausente", ql["sem_data"], ok(ql["sem_data"]), "Nenhum caso"),
            ("Registros duplicados", dup if dup is not None else 0,
             ok(dup) if not dup else "ℹ️ %s do total" % pq(dup),
             "A carga não repete arquivos; os casos restantes já vêm repetidos no arquivo da NASA e são mantidos (efeito desprezível)"
             if dup else "A carga registra cada arquivo e não carrega o mesmo duas vezes"),
            ("FRP ausente", ql["frp_nulo"], ok(ql["frp_nulo"]), "Ficam na classe \"Sem FRP\" e fora da média de FRP"),
            ("FRP negativo (ruído do sensor)", ql["frp_negativo"], "ℹ️ %s do total" % pq(ql["frp_negativo"]),
             "Mantidos como no dado da NASA, na classe \"Muito baixa\"; efeito desprezível na soma"),
            ("Tipo de fonte não informado", ql["tipo_nulo"], "ℹ️ %s do total" % pct(ql["tipo_nulo"], orig),
             "Os arquivos NRT não trazem o tipo; aparecem como \"Não informado (NRT)\""),
        ], columns=["Verificação", "Linhas", "Situação", "Tratamento"])
        st.dataframe(chk, width="stretch", hide_index=True,
                     column_config={"Linhas": st.column_config.NumberColumn(format="localized"),
                                    "Tratamento": st.column_config.TextColumn(width="large")})
    else:
        st.info("Rode de novo `python ferramentas/exportar_dados.py` para gerar as verificações de qualidade.")

    st.markdown("**Atualização**")
    st.markdown("- Última exportação: **%s** · dados de %s a %s.\n"
                "- Frequência recomendada: **diária** — `carregar_csv.py` com o arquivo novo do FIRMS e depois "
                "`exportar_dados.py` (só as linhas novas são processadas).\n"
                "- **NRT** (tempo quase real) sai ~3 h após a passagem do satélite e é provisório; o **archive** "
                "(definitivo) sai meses depois e substitui o NRT do mesmo período na carga."
                % (INFO["gerado_em"], data_br(D0), data_br(D1)))
    fontes = INFO.get("fontes") or []
    if fontes:
        with st.expander("Arquivos carregados no MySQL (%d)" % len(fontes)):
            ft = pd.DataFrame(fontes)
            ft["fonte"] = ft.fonte.map({"archive": "Definitivo (archive)", "nrt": "Tempo quase real (NRT)"}).fillna(ft.fonte)
            st.dataframe(ft, width="stretch", hide_index=True,
                         column_config={"arquivo": "Arquivo", "fonte": "Tipo", "linhas": st.column_config.NumberColumn("Linhas", format="localized"),
                                        "de": "De", "ate": "Até", "em": "Carregado em"})
            st.caption("Os dados NRT (tempo quase real) são provisórios; quando a NASA publica os definitivos (archive) "
                       "do mesmo período, a carga substitui os NRT.")

    st.markdown("**Regras das métricas** (as mesmas em todo o painel)")
    st.markdown("""
| Métrica | Definição |
|---|---|
| **Foco de calor** | Um pixel (≈ 375 m no VIIRS, 1 km no MODIS) em que o satélite detectou temperatura anômala numa passagem. Não é o mesmo que um incêndio: um incêndio grande gera vários focos. |
| **Data** | Data da passagem em UTC (Brasília = UTC − 3). |
| **Situação** | Focos dos últimos 7 dias do período ÷ média semanal de todo o período carregado, para a mesma região e filtros. 🔴 Alerta ≥ 1,5× · 🟡 Atenção 1× a 1,5× · 🟢 Normal < 1×. |
| **FRP** | Potência radiativa do fogo (MW): indica a intensidade. Classes: muito baixa < 2, baixa 2–5, moderada 5–20, alta 20–100, muito alta ≥ 100 MW. |
| **Confiança** | Classificação do satélite: baixa, nominal ou alta. Baixa pode ser reflexo do sol ou superfície quente. |
| **Dia / noite** | Período da passagem do satélite. |
| **País / estado** | Pela célula de 0,1° (≈ 11 km) em que o foco cai (Natural Earth). |
| **Hora solar local** | Hora UTC + longitude ÷ 15. |
""")

with aba[6]:
    st.markdown("**Focos por dia e país** com os filtros atuais")
    tab = consulta(f"SELECT dia, pais, SUM({Q}) focos, ROUND(SUM({FS}), 2) frp_total_mw, ROUND(MAX({FM}), 2) frp_max_mw "
                   f"FROM f WHERE {W} GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    tab.insert(2, "pais_nome", tab.pais.map(lambda c: nome_pais(int(c))))
    st.dataframe(tab.drop(columns=["pais"]).tail(5000), width="stretch", hide_index=True, height=360,
                 column_config={"dia": st.column_config.DateColumn("Dia", format="DD/MM/YYYY"),
                                "pais_nome": "País", "focos": st.column_config.NumberColumn("Focos", format="localized"),
                                "frp_total_mw": st.column_config.NumberColumn("FRP total (MW)", format="localized"),
                                "frp_max_mw": st.column_config.NumberColumn("FRP máximo (MW)", format="localized")})
    st.download_button("Baixar CSV completo (%s linhas)" % fmt(len(tab)),
                       tab.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       file_name="focos_por_dia_e_pais.csv", mime="text/csv")

st.caption("Dados: NASA FIRMS (MODIS e VIIRS), exportados da tabela %s do banco %s. FRP = potência radiativa do fogo, em megawatts. "
           "Base cartográfica: Natural Earth (domínio público)." % (INFO["tabela"], INFO["banco"]))
