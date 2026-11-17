import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

# Configuração da página
st.set_page_config(page_title="Dashboard Básico", layout="wide", page_icon="📊")


# Dados de exemplo (vendas fictícias). Troque por pd.read_csv("seu_arquivo.csv") quando quiser.
@st.cache_data
def carregar_dados():
    rng = np.random.default_rng(42)
    datas = pd.date_range("2025-01-01", "2025-12-31", freq="D")
    n = len(datas) * 3
    df = pd.DataFrame({
        "Data": np.repeat(datas, 3),
        "Região": rng.choice(["Norte", "Nordeste", "Sudeste", "Sul", "Centro-Oeste"], n),
        "Categoria": rng.choice(["Eletrônicos", "Roupas", "Alimentos", "Livros"], n),
        "Quantidade": rng.integers(1, 20, n),
        "Preço": rng.uniform(10, 500, n).round(2),
    })
    df["Faturamento"] = (df["Quantidade"] * df["Preço"]).round(2)
    return df


df = carregar_dados()

# --- BARRA LATERAL (FILTROS) ---
st.sidebar.header("🔍 Filtros")

data_min, data_max = df["Data"].min().date(), df["Data"].max().date()
periodo = st.sidebar.date_input("Período:", (data_min, data_max), min_value=data_min, max_value=data_max)

regioes = sorted(df["Região"].unique())
regioes_sel = st.sidebar.multiselect("Região:", regioes, default=regioes)

categorias = sorted(df["Categoria"].unique())
categorias_sel = st.sidebar.multiselect("Categoria:", categorias, default=categorias)

# Aplica os filtros
inicio, fim = (periodo if len(periodo) == 2 else (periodo[0], periodo[0]))
df_f = df[
    df["Data"].between(pd.Timestamp(inicio), pd.Timestamp(fim))
    & df["Região"].isin(regioes_sel)
    & df["Categoria"].isin(categorias_sel)
]

# --- CORPO ---
st.title("📊 Dashboard de Vendas")
st.caption("Exemplo básico em Streamlit com dados fictícios.")

if df_f.empty:
    st.warning("Nenhum dado para os filtros selecionados.")
    st.stop()

def reais(valor):
    """Formata em reais de forma compacta: R$ 2,7 mi / R$ 15,3 mil / R$ 950."""
    if valor >= 1_000_000:
        texto = f"{valor / 1_000_000:.1f} mi"
    elif valor >= 1_000:
        texto = f"{valor / 1_000:.1f} mil"
    else:
        texto = f"{valor:.0f}"
    return "R$ " + texto.replace(".", ",")


# Cards (KPIs)
col1, col2, col3, col4 = st.columns(4)
col1.metric("Faturamento total", reais(df_f["Faturamento"].sum()))
col2.metric("Pedidos", f"{len(df_f):,}".replace(",", "."))
col3.metric("Itens vendidos", f"{df_f['Quantidade'].sum():,}".replace(",", "."))
col4.metric("Ticket médio", reais(df_f["Faturamento"].mean()))

st.divider()

# Gráficos
aba1, aba2 = st.tabs(["📈 Gráficos", "📋 Dados"])

with aba1:
    mensal = df_f.groupby(df_f["Data"].dt.to_period("M").dt.to_timestamp())["Faturamento"].sum().reset_index()
    fig_linha = px.line(mensal, x="Data", y="Faturamento", markers=True, title="Faturamento mensal")
    st.plotly_chart(fig_linha, use_container_width=True)

    c1, c2 = st.columns(2)
    por_regiao = df_f.groupby("Região", as_index=False)["Faturamento"].sum().sort_values("Faturamento")
    c1.plotly_chart(
        px.bar(por_regiao, x="Faturamento", y="Região", orientation="h", title="Faturamento por região"),
        use_container_width=True,
    )
    por_categoria = df_f.groupby("Categoria", as_index=False)["Faturamento"].sum()
    c2.plotly_chart(
        px.pie(por_categoria, names="Categoria", values="Faturamento", hole=0.4, title="Participação por categoria"),
        use_container_width=True,
    )

with aba2:
    st.dataframe(df_f.sort_values("Data", ascending=False), use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Baixar CSV",
        df_f.to_csv(index=False).encode("utf-8-sig"),
        file_name="vendas_filtradas.csv",
        mime="text/csv",
    )
