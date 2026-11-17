import pandas as pd

# Região de cada UF a partir do primeiro dígito do código do IBGE
REGIOES = {"1": "Norte", "2": "Nordeste", "3": "Sudeste", "4": "Sul", "5": "Centro-Oeste"}


def tratar_dados():
    # Leitura das duas tabelas (separador ";" e vírgula decimal)
    tab_2012 = pd.read_csv("tables/Tabela5-sem_emprego_2012.csv", sep=";", decimal=",", encoding="utf-8-sig")
    tab_2026 = pd.read_csv("tables/Tabela5-sem_emprego_2026.csv", sep=";", decimal=",", encoding="utf-8-sig")

    # Nomes de coluna mais curtos
    tab_2012 = tab_2012.rename(columns={"Desocupados - homens (2012 T1)": "homens_2012",
                                        "Desocupados - mulheres (2012 T1)": "mulheres_2012"})
    tab_2026 = tab_2026.rename(columns={"Desocupados - homens (2026 T1)": "homens_2026",
                                        "Desocupados - mulheres (2026 T1)": "mulheres_2026"})

    # Junta os dois anos em uma tabela só (uma linha por UF)
    comp = tab_2012.merge(tab_2026, on=["Sigla", "Código", "Estado"], how="inner")
    comp["Região"] = comp["Código"].astype(str).str[0].map(REGIOES)

    # Variação em pontos percentuais entre 2012 e 2026
    comp["variacao_homens"] = comp["homens_2026"] - comp["homens_2012"]
    comp["variacao_mulheres"] = comp["mulheres_2026"] - comp["mulheres_2012"]

    # --- Tabelas menores, cada uma pronta para um uso no dashboard ---

    # 1) Participação: uma linha por UF, ano e sexo (gráficos de barras, histograma e boxplot)
    participacao = comp.melt(
        id_vars=["Sigla", "Estado", "Região"],
        value_vars=["homens_2012", "mulheres_2012", "homens_2026", "mulheres_2026"],
        var_name="coluna",
        value_name="Participação (%)",
    )
    participacao["Sexo"] = participacao["coluna"].str.split("_").str[0].map({"homens": "Homens", "mulheres": "Mulheres"})
    participacao["Ano"] = participacao["coluna"].str.split("_").str[1]
    participacao = participacao.drop(columns="coluna")

    # 2) Variação: uma linha por UF e sexo (pergunta 3)
    variacao = comp.melt(
        id_vars=["Sigla", "Estado", "Região"],
        value_vars=["variacao_homens", "variacao_mulheres"],
        var_name="coluna",
        value_name="Variação (p.p.)",
    )
    variacao["Sexo"] = variacao["coluna"].map({"variacao_homens": "Homens", "variacao_mulheres": "Mulheres"})
    variacao = variacao.drop(columns="coluna")

    # 3) Tabela completa (uma linha por UF) para a aba de dados
    tabela = comp[["Sigla", "Estado", "Região", "homens_2012", "mulheres_2012", "homens_2026", "mulheres_2026",
                   "variacao_homens", "variacao_mulheres"]]

    return participacao, variacao, tabela


if __name__ == "__main__":
    participacao, variacao, tabela = tratar_dados()
    print(participacao.head())
    print(variacao.head())
    print(tabela.head())
