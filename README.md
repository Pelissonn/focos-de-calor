# Focos de calor — dashboard em Streamlit

Dashboard cartográfico dos focos de calor da NASA FIRMS (MODIS e VIIRS): mapa mundial com detalhe por
continente, país e estado, intensidade (FRP), série temporal, calendário mês a mês e categorias.

## Objetivo

- **Para quem:** gestores de defesa civil, órgãos ambientais e coordenadores de brigadas de incêndio.
- **Pergunta central:** onde os focos de calor estão acima do normal agora — e onde concentrar equipes
  de combate e fiscalização?
- **Ação:** a situação compara os focos dos últimos 7 dias com a média semanal da mesma região
  (🔴 Alerta ≥ 1,5× → acionar brigadas e fiscalização; 🟡 Atenção 1×–1,5× → prontidão e monitoramento
  diário; 🟢 Normal → rotina e prevenção). A aba **🚨 Alertas** (MVP) lista os estados/províncias acima do
  normal, ordenados pelo excesso de focos, com download em CSV.

## Como funciona

O Streamlit Community Cloud roda na nuvem e **não acessa o MySQL do seu computador**. Por isso os dados
vão para o GitHub em arquivos Parquet compactos (`dados/focos/`), gerados a partir do MySQL por um script
que você roda no seu PC. O app lê esses arquivos com DuckDB.

```
streamlit_app.py            o app
requirements.txt            bibliotecas do app (o Streamlit Cloud instala sozinho)
.streamlit/config.toml      tema
dados/mapa_base.json        países, estados e cidades (Natural Earth, domínio público)
dados/focos/*.parquet       focos de calor   <- gerados pelo exportador
dados/info.json             período, satélites etc. <- gerado pelo exportador
ferramentas/                exportador (roda só no seu PC, não vai para a nuvem)
```

## 0. Carregar os CSV do FIRMS no MySQL

Baixe os dados em https://firms.modaps.eosdis.nasa.gov/download/ (formato CSV) e, com o MySQL ligado
(no WAMP, ícone verde), rode na pasta do projeto:

```bash
pip install pymysql
python ferramentas/carregar_csv.py C:/caminho/DL_FIRE_J1V-C2_833522.zip
```

- Aceita o `.zip` baixado, arquivos `.csv` soltos ou uma pasta com vários CSV (MODIS ou VIIRS, archive ou NRT).
- Cria o banco `area_queimada` e a tabela `firms_tratado` com as colunas tratadas que o dashboard usa
  (data/hora UTC, confiança Baixa/Nominal/Alta, Dia/Noite, tipo de fonte, classe de FRP) e registra cada
  arquivo na tabela `firms_carga` — rodar de novo não duplica (use `--recarregar` para refazer um arquivo).
- Ao carregar um arquivo *archive* (definitivo), os focos *NRT* (provisórios) do mesmo satélite e período
  são substituídos (desligue com `--manter-nrt`).
- Usa `LOAD DATA LOCAL INFILE`; se o `local_infile` estiver desligado, o script tenta ligá-lo.

## 1. Gerar os dados (no seu computador, onde está o MySQL)

Na pasta do projeto:

```bash
pip install pymysql pyarrow duckdb
python ferramentas/exportar_dados.py
```

- Usa por padrão `localhost`, usuário `root` sem senha e o banco `area_queimada`
  (mude com `--host`, `--user`, `--database`; senha pela variável `MYSQL_PASSWORD`).
- Na primeira vez ele prepara as tabelas `firms_dash_*` no banco (mostra o progresso; a `firms_tratado`
  não é alterada) e depois grava `dados/focos/`. Os arquivos ficam abaixo de 100 MB cada (limite do GitHub).
- Se os focos individuais passarem de 700 MB, ele grava uma grade de 0,1° (≈ 11 km) por dia, bem menor.
- Muito grande ainda? Exporte só um período: `python ferramentas/exportar_dados.py --desde 2015-01-01`.

Para ver o app no seu PC antes de publicar:

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 2. Enviar para o GitHub

Crie um repositório vazio no GitHub (por exemplo `focos-de-calor`) e, na pasta do projeto:

```bash
git init -b main
git add .
git commit -m "Dashboard de focos de calor"
git remote add origin https://github.com/SEU-USUARIO/focos-de-calor.git
git push -u origin main
```

(Ou use o GitHub Desktop: *Add local repository* → *Publish repository*.)

## 3. Publicar no Streamlit Community Cloud

1. Entre em https://share.streamlit.io com a sua conta do GitHub.
2. **Create app** → *Deploy a public app from GitHub*.
3. Repositório: `SEU-USUARIO/focos-de-calor` · Branch: `main` · Arquivo: `streamlit_app.py`.
4. Em *Advanced settings*, escolha Python **3.12**.
5. **Deploy**. Em alguns minutos o app fica em `https://<nome>.streamlit.app`.

## Atualizar os dados depois

```bash
python ferramentas/exportar_dados.py
git add dados
git commit -m "Atualiza dados"
git push
```

O Streamlit Cloud percebe o push e reinicia o app com os dados novos.

## Recursos do app

- Filtros na barra lateral: período, satélite, confiança, dia/noite, tipo de fonte, classe de FRP,
  continente, país e estado.
- Mapa: células coloridas pela medida escolhida (focos, FRP total, médio ou máximo), em escala
  logarítmica; a resolução fica mais fina ao escolher continente, país ou estado. Com poucos focos
  (até 15 mil, num país ou estado), mostra cada foco individualmente. Clique numa célula para filtrar pelo país.
- Página em três níveis: **topo** com público, pergunta, manchete e KPIs com situação (vermelho/amarelo/verde);
  **meio** com a evolução no tempo (com a média como referência) e o mapa; **base** com as abas de detalhe.
- Abas: 🚨 Alertas (áreas acima do normal e ação sugerida), ranking de países (ou de estados), calendário
  ano × mês, horário (hora solar local, hora × mês e dia da semana), categorias, qualidade dos dados
  (verificações, atualização, arquivos carregados e regras das métricas) e tabela com download em CSV.
- Links compartilháveis: os filtros de período e região ficam na URL, por exemplo
  `?pais=76&de=2020-08&ate=2020-09` (Brasil, ago–set de 2020).

Cada foco é atribuído ao país e ao estado pela célula de 0,1° (≈ 11 km) em que cai; focos no mar a mais
de ~30 km da costa aparecem como "sem país". Datas em UTC. Mapa de fundo: © CARTO © OpenStreetMap.
