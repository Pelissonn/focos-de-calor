import streamlit as st
import pandas as pd
import plotly.express as px

from tratamento import tratar_dados

# Configuração da página
st.set_page_config(page_title="Trabalho e Emprego", layout="wide", page_icon="👷")

# Cores fixas para os anos e para os sexos
CORES_ANO = {"2012": "#9AA5B1", "2026": "#1F5FA8"}
CORES_SEXO = {"Homens": "#1f77b4", "Mulheres": "#e377c2"}


# Memória temporária - se não colocar o cache_data vai pesar o streamlit
@st.cache_data
def carregar_dados():
    return tratar_dados()


try:
    participacao, variacao, tabela = carregar_dados()

    # --- BARRA LATERAL (SIDEBAR) ---
    st.sidebar.header("🔍 Filtros")

    sexo_selecionado = st.sidebar.selectbox("Sexo:", ["Mulheres", "Homens"])

    regioes = ["Norte", "Nordeste", "Sudeste", "Sul", "Centro-Oeste"]
    regioes_selecionadas = st.sidebar.multiselect("Região:", regioes, default=regioes)

    ufs_disponiveis = sorted(tabela.loc[tabela["Região"].isin(regioes_selecionadas), "Sigla"])
    ufs_selecionadas = st.sidebar.multiselect("UF (vazio = todas):", ufs_disponiveis)
    if not ufs_selecionadas:
        ufs_selecionadas = ufs_disponiveis

    # Filtrar DataFrames com base nas seleções
    part_f = participacao[participacao["Sigla"].isin(ufs_selecionadas)]
    part_sexo = part_f[part_f["Sexo"] == sexo_selecionado]
    var_sexo = variacao[variacao["Sigla"].isin(ufs_selecionadas) & (variacao["Sexo"] == sexo_selecionado)]
    tabela_f = tabela[tabela["Sigla"].isin(ufs_selecionadas)]

    # --- CORPO DA APLICAÇÃO ---
    st.title("👷 Trabalho e População Empregada")
    st.caption("Participação de homens e mulheres entre as pessoas desocupadas, por UF - 1º trimestre de 2012 e de 2026.")

    if tabela_f.empty:
        st.warning("Nenhuma UF selecionada nos filtros da barra lateral.")
        st.stop()

    # Valores do sexo escolhido em cada ano
    valores_2012 = part_sexo.loc[part_sexo["Ano"] == "2012", "Participação (%)"]
    valores_2026 = part_sexo.loc[part_sexo["Ano"] == "2026", "Participação (%)"]

    # --- CARDS ---
    maioria_2012 = (tabela_f["mulheres_2012"] > 50).sum()
    maioria_2026 = (tabela_f["mulheres_2026"] > 50).sum()
    maior_uf = part_sexo[part_sexo["Ano"] == "2026"].sort_values("Participação (%)", ascending=False).iloc[0]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(f"Média de {sexo_selecionado.lower()} (2026)", f"{valores_2026.mean():.1f}%",
                f"{valores_2026.mean() - valores_2012.mean():.1f} p.p. vs 2012", delta_color="off")
    col2.metric("Mediana (2026)", f"{valores_2026.median():.1f}%")
    col3.metric("UFs com maioria feminina (2026)", f"{maioria_2026} de {len(tabela_f)}",
                f"{maioria_2026 - maioria_2012} vs 2012", delta_color="inverse")
    col4.metric("Maior participação (2026)", maior_uf["Sigla"], f"{maior_uf['Participação (%)']:.1f}%",
                delta_color="off")

    st.markdown("---")

    aba1, aba2, aba3 = st.tabs(["📉 Desocupados por UF", "📊 Análise estatística", "📰 Notícias e fotos"])

    # =========================== ABA 1 ===========================
    with aba1:
        # Ordem das UFs: maior participação em 2026 primeiro
        ordem = part_sexo[part_sexo["Ano"] == "2026"].sort_values("Participação (%)", ascending=False)["Sigla"]

        # Pergunta 1: comparação 2012 x 2026 por UF
        st.subheader(f"1) Participação de {sexo_selecionado.lower()} entre os desocupados por UF: 2012 x 2026")
        fig_uf = px.bar(
            part_sexo,
            x="Sigla",
            y="Participação (%)",
            color="Ano",
            barmode="group",
            title=f"{sexo_selecionado} entre as pessoas desocupadas (%)",
            category_orders={"Sigla": list(ordem), "Ano": ["2012", "2026"]},
            color_discrete_map=CORES_ANO,
            labels={"Sigla": "UF"},
        )
        fig_uf.add_hline(y=50, line_dash="dash", line_color="gray", annotation_text="50% (paridade)")
        st.plotly_chart(fig_uf, width="stretch")

        # Pergunta 2: por gênero
        st.subheader("2) Homens x mulheres entre os desocupados por UF: 2012 x 2026")
        fig_genero = px.bar(
            part_f,
            x="Sigla",
            y="Participação (%)",
            color="Sexo",
            barmode="group",
            facet_row="Ano",
            height=600,
            title="Composição dos desocupados por sexo",
            category_orders={"Sigla": list(ordem), "Ano": ["2012", "2026"]},
            color_discrete_map=CORES_SEXO,
            labels={"Sigla": "UF"},
        )
        fig_genero.add_hline(y=50, line_dash="dash", line_color="gray")
        st.plotly_chart(fig_genero, width="stretch")

        # Pergunta 3: qual estado mais reduziu
        st.subheader(f"3) Em qual estado a participação de {sexo_selecionado.lower()} entre os desocupados mais caiu?")
        var_ordenada = var_sexo.sort_values("Variação (p.p.)").copy()
        var_ordenada["Resultado"] = var_ordenada["Variação (p.p.)"].apply(lambda v: "Caiu" if v < 0 else "Subiu")

        fig_var = px.bar(
            var_ordenada,
            x="Variação (p.p.)",
            y="Sigla",
            color="Resultado",
            orientation="h",
            height=650,
            title=f"Variação da participação de {sexo_selecionado.lower()} entre os desocupados (2026 - 2012)",
            color_discrete_map={"Caiu": "#2A9D8F", "Subiu": "#E76F51"},
            hover_name="Estado",
            labels={"Sigla": "UF"},
        )
        fig_var.update_yaxes(categoryorder="total descending")  # maior queda no topo
        st.plotly_chart(fig_var, width="stretch")

        melhor = var_ordenada.iloc[0]  # primeira linha = maior queda
        st.success(
            f"**{melhor['Estado']}** teve a maior queda na participação de {sexo_selecionado.lower()} "
            f"entre os desocupados: {melhor['Variação (p.p.)']:.1f} p.p. entre 2012 e 2026."
        )

    # =========================== ABA 2 ===========================
    with aba2:
        st.subheader(f"Distribuição da participação de {sexo_selecionado.lower()} entre os desocupados")
        ano_hist = st.radio("Ano:", ["2026", "2012"], horizontal=True)
        df_hist = part_sexo[part_sexo["Ano"] == ano_hist]
        media = df_hist["Participação (%)"].mean()
        mediana = df_hist["Participação (%)"].median()

        col1, col2 = st.columns([3, 1])
        with col1:
            # Histograma com média e mediana
            fig_hist = px.histogram(
                df_hist,
                x="Participação (%)",
                nbins=12,
                title=f"Histograma - {sexo_selecionado} entre os desocupados ({ano_hist})",
                color_discrete_sequence=[CORES_ANO[ano_hist]],
            )
            fig_hist.update_layout(yaxis_title="Número de UFs")
            fig_hist.add_vline(x=media, line_dash="dash", line_color="red", annotation_text="Média")
            fig_hist.add_vline(x=mediana, line_color="green", annotation_text="Mediana",
                               annotation_position="top left")
            st.plotly_chart(fig_hist, width="stretch")
        with col2:
            st.metric("Média", f"{media:.1f}%")
            st.metric("Mediana", f"{mediana:.1f}%")
            st.metric("Desvio-padrão", f"{df_hist['Participação (%)'].std():.1f} p.p.")

        if media > mediana:
            st.info("A média está **acima** da mediana: alguns estados com valores muito altos puxam a média "
                    "para cima (assimetria à direita).")
        elif media < mediana:
            st.info("A média está **abaixo** da mediana: alguns estados com valores muito baixos puxam a média "
                    "para baixo (assimetria à esquerda).")
        else:
            st.info("Média e mediana são iguais: a distribuição é simétrica.")

        # Boxplot para identificar outliers
        st.subheader("Outliers: boxplot 2012 x 2026")
        col1, col2 = st.columns([2, 1])
        with col1:
            fig_box = px.box(
                part_sexo,
                x="Ano",
                y="Participação (%)",
                color="Ano",
                points="all",
                hover_name="Estado",
                title=f"{sexo_selecionado} entre os desocupados - boxplot por ano",
                category_orders={"Ano": ["2012", "2026"]},
                color_discrete_map=CORES_ANO,
            )
            st.plotly_chart(fig_box, width="stretch")
        with col2:
            resumo = part_sexo.groupby("Ano")["Participação (%)"].agg(["mean", "median", "min", "max"]).round(1)
            resumo.columns = ["Média", "Mediana", "Mínimo", "Máximo"]
            st.dataframe(resumo, width="stretch")
            st.markdown(
                "Os pontos fora das \"caixas\" do boxplot são os **outliers**: estados com valor muito "
                "diferente dos demais. Passe o mouse sobre os pontos para ver o estado."
            )

    # =========================== ABA 3 ===========================
    with aba3:
        st.subheader("📰 Notícias recentes")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### IBGE - PNAD Contínua (29/09/2026)")
            st.markdown("**Taxa de desocupação é de 5,3% e taxa de subutilização é de 13,1% no trimestre encerrado em agosto**")
            st.markdown(
                "- Desocupação caiu de 5,6% para 5,3% no trimestre.\n"
                "- 5,8 milhões de desocupados (menos 281 mil pessoas).\n"
                "- 103,5 milhões de ocupados, o maior número da série histórica."
            )
            st.image("images/IBGEindicadorEmprego_recorte.png", width="stretch")
            st.link_button("Agência IBGE Notícias", "https://agenciadenoticias.ibge.gov.br/")
        with col2:
            st.markdown("#### Novo CAGED - MTE (29/09/2026)")
            st.markdown("**Emprego formal gera 165,8 mil vagas em agosto e acumula 1,13 milhão de postos no ano**")
            st.markdown(
                "- 48,2 milhões de vínculos com carteira assinada.\n"
                "- Serviços lideram no ano (+697,9 mil vagas), seguidos pela Construção (+200,4 mil).\n"
                "- Saldo positivo em todos os grandes setores em agosto."
            )
            st.image("images/CagedEmprego_recorte.png", width="stretch")
            st.link_button("Painel do Novo CAGED",
                           "https://www.gov.br/trabalho-e-emprego/pt-br/assuntos/estatisticas-trabalho/novo-caged")

        st.markdown("---")
        st.subheader("📷 Ambientes de trabalho no Brasil")
        col1, col2 = st.columns(2)
        col1.image("images/Ambiente_trabalho1_recorte.png", width="stretch", caption="Escritório - setor de serviços")
        col2.image("images/Ambiente_trabalho2_recorte.png", width="stretch", caption="Linha de produção - indústria")

except Exception as e:
    st.error(f"Erro ao carregar ou processar os dados: {e}")
    st.info("Certifique-se de que as pastas 'tables' e 'images' estão no mesmo diretório do app.py.")