import datetime
import io
import shutil
import sqlite3
import time
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(
    page_title="Controle de Motorista de App",
    page_icon="🚗",
    layout="wide",
)

# --- CONFIGURAÇÕES ---
DB_PATH = Path("motorista.db")
PASTA_BACKUP = Path("Backup")
INTERVALO_BACKUP = 1800  # 30 minutos
ARQUIVO_APP = Path(__file__).resolve()

COLUNAS_NUMERICAS = [
    "ganhos_99",
    "ganhos_id",
    "custo_passe",
    "horas",
    "km_ini",
    "km_fin",
    "kw_ini",
    "kw_fin",
    "preco_kw",
]

COLUNAS_BANCO = [
    "data",
    "dia_semana",
    "is_folga",
    "ganhos_99",
    "ganhos_id",
    "custo_passe",
    "horas",
    "km_ini",
    "km_fin",
    "kw_ini",
    "kw_fin",
    "preco_kw",
]

MESES_PT = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Março",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}


# --- FUNÇÕES DE FORMATAÇÃO E UTILIDADE ---
def formatar_moeda(valor):
    """Formata um número como moeda no padrão brasileiro."""
    valor = float(valor or 0)
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def valor_inicial(registro, coluna, zero_vazio=True):
    """Obtém o valor inicial de um campo do registro sem repetir validações."""
    if registro is None:
        return None

    valor = pd.to_numeric(registro.get(coluna), errors="coerce")
    if pd.isna(valor):
        return None

    if zero_vazio and float(valor) == 0:
        return None

    return float(valor)


# --- BANCO DE DADOS ---
def conectar_banco():
    """Abre a conexão SQLite com configurações de segurança/confiabilidade."""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def inicializar_banco(conn):
    """Cria a tabela e garante compatibilidade com versões anteriores."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lancamentos (
            data TEXT PRIMARY KEY,
            dia_semana TEXT,
            is_folga INTEGER DEFAULT 0,
            ganhos_99 REAL DEFAULT 0,
            ganhos_id REAL DEFAULT 0,
            custo_passe REAL DEFAULT 0,
            horas REAL DEFAULT 0,
            km_ini REAL DEFAULT 0,
            km_fin REAL DEFAULT 0,
            kw_ini REAL DEFAULT 0,
            kw_fin REAL DEFAULT 0,
            preco_kw REAL DEFAULT 0
        )
        """
    )

    colunas = {
        row[1] for row in conn.execute("PRAGMA table_info(lancamentos)").fetchall()
    }
    if "is_folga" not in colunas:
        conn.execute(
            "ALTER TABLE lancamentos ADD COLUMN is_folga INTEGER DEFAULT 0"
        )

    conn.commit()


def carregar_lancamentos(conn):
    """Carrega todos os lançamentos do banco em um DataFrame."""
    return pd.read_sql_query(
        """
        SELECT data, dia_semana, is_folga, ganhos_99, ganhos_id, custo_passe,
               horas, km_ini, km_fin, kw_ini, kw_fin, preco_kw
        FROM lancamentos
        """,
        conn,
    )


def salvar_lancamento(conn, dados):
    """Insere ou atualiza um lançamento usando parâmetros SQL."""
    conn.execute(
        """
        INSERT OR REPLACE INTO lancamentos
        (data, dia_semana, is_folga, ganhos_99, ganhos_id, custo_passe,
         horas, km_ini, km_fin, kw_ini, kw_fin, preco_kw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(dados[coluna] for coluna in COLUNAS_BANCO),
    )
    conn.commit()


def apagar_lancamento(conn, data):
    """Apaga somente o lançamento da data informada."""
    conn.execute("DELETE FROM lancamentos WHERE data = ?", (data,))
    conn.commit()


def apagar_todos_lancamentos(conn):
    """Apaga todos os lançamentos."""
    conn.execute("DELETE FROM lancamentos")
    conn.commit()


# --- CÁLCULOS ---
def preparar_dados(df):
    """Converte as colunas numéricas do banco e propaga o preço do KW automaticamente."""
    df = df.copy()

    if df.empty:
        return df

    for coluna in COLUNAS_NUMERICAS:
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce").fillna(0)

    df["preco_kw"] = df["preco_kw"].replace(0, pd.NA).ffill().fillna(0)
    df["is_folga"] = pd.to_numeric(df["is_folga"], errors="coerce").fillna(0).astype(int)
    return df


def calcular_indicadores(df):
    """Calcula todos os indicadores derivados de cada lançamento."""
    df = preparar_dados(df)

    if df.empty:
        return df

    df["Total Dia"] = df["ganhos_99"] + df["ganhos_id"]
    df["KW Consumido"] = (df["kw_fin"] - df["kw_ini"]).clip(lower=0)
    df["Energia"] = df["KW Consumido"] * df["preco_kw"]
    df["Custo Total"] = df["custo_passe"] + df["Energia"]
    df["Lucro Líquido"] = df["Total Dia"] - df["Custo Total"]
    df["KM Rodados"] = (df["km_fin"] - df["km_ini"]).clip(lower=0)

    horas_validas = df["horas"].where(df["horas"] > 0)
    km_validos = df["KM Rodados"].where(df["KM Rodados"] > 0)

    df["R$ / Hora"] = (df["Lucro Líquido"] / horas_validas).fillna(0)
    df["R$ / km"] = (
        (df["Total Dia"] - df["custo_passe"]) / km_validos
    ).fillna(0)

    return df


def calcular_resumo(df):
    """Calcula os indicadores gerais do período."""
    if df.empty:
        return {
            "faturamento": 0.0,
            "lucro": 0.0,
            "horas": 0.0,
            "passe": 0.0,
            "energia": 0.0,
            "km": 0.0,
            "rs_km": 0.0,
            "rs_dia": 0.0,
            "rs_hora": 0.0,
            "dias_trabalhados": 0,
            "lucro_medio_dia": 0.0,
        }

    faturamento = df["Total Dia"].sum()
    lucro = df["Lucro Líquido"].sum()
    horas = df["horas"].sum()
    passe = df["custo_passe"].sum()
    energia = df["Energia"].sum()
    km = df["KM Rodados"].sum()
    dias_trabalhados = int((df["is_folga"] == 0).sum())

    return {
        "faturamento": faturamento,
        "lucro": lucro,
        "horas": horas,
        "passe": passe,
        "energia": energia,
        "km": km,
        "rs_km": ((df["Total Dia"] - df["custo_passe"]).sum() / km)
        if km > 0
        else 0.0,
        "rs_dia": (faturamento / dias_trabalhados)
        if dias_trabalhados > 0
        else 0.0,
        "rs_hora": (lucro / horas) if horas > 0 else 0.0,
        "dias_trabalhados": dias_trabalhados,
        "lucro_medio_dia": (lucro / dias_trabalhados)
        if dias_trabalhados > 0
        else 0.0,
    }


# --- BACKUPS ---
def copiar_arquivo_com_timestamp(origem, pasta, timestamp):
    """Copia um arquivo para a pasta de backup com timestamp."""
    if not origem.exists():
        return None

    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"{origem.stem}_{timestamp}{origem.suffix}"
    contador = 1
    while destino.exists():
        destino = pasta / f"{origem.stem}_{timestamp}_{contador}{origem.suffix}"
        contador += 1
    shutil.copy2(origem, destino)
    return destino


def fazer_backup_banco(conn, timestamp):
    """Cria backup consistente do SQLite usando o mecanismo nativo do SQLite."""
    PASTA_BACKUP.mkdir(parents=True, exist_ok=True)
    destino = PASTA_BACKUP / f"motorista_{timestamp}.db"
    contador = 1
    while destino.exists():
        destino = PASTA_BACKUP / f"motorista_{timestamp}_{contador}.db"
        contador += 1

    backup_conn = sqlite3.connect(destino)
    try:
        conn.commit()
        conn.backup(backup_conn)
    finally:
        backup_conn.close()

    return destino


def salvar_backup_arquivos(conn):
    """Faz backup consistente do banco e do código da aplicação."""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    destinos = []

    try:
        destinos.append(fazer_backup_banco(conn, timestamp))
        codigo = copiar_arquivo_com_timestamp(ARQUIVO_APP, PASTA_BACKUP, timestamp)
        if codigo:
            destinos.append(codigo)
    except (OSError, sqlite3.Error) as erro:
        raise RuntimeError(f"Não foi possível concluir o backup: {erro}") from erro

    return destinos


def fazer_backup_automatico(conn):
    """Executa o backup automático, no máximo uma vez a cada 30 minutos."""
    agora = time.time()
    ultimo = st.session_state.get("ultimo_backup", 0)

    if agora - ultimo < INTERVALO_BACKUP:
        return

    try:
        salvar_backup_arquivos(conn)
        st.session_state.ultimo_backup = agora
    except RuntimeError as erro:
        print(f"Erro no backup automático: {erro}")


# --- DIAS DA SEMANA ---
DIAS_PT = {
    "Mon": "SEG.",
    "Tue": "TER.",
    "Wed": "QUA.",
    "Thu": "QUI.",
    "Fri": "SEX.",
    "Sat": "SÁB.",
    "Sun": "DOM.",
}


# --- FORMATAÇÃO DA TABELA ---
def preparar_tabela_exibicao(df):
    """Monta a tabela visual sem usar apply linha a linha."""
    if df.empty:
        return df

    dados = df.copy()
    folga = dados["is_folga"].eq(1)

    exibicao = pd.DataFrame(index=dados.index)
    exibicao["Data"] = dados["data"]
    exibicao["Dia"] = dados["dia_semana"]

    def moeda_sem_simbolo(serie):
        return serie.map(lambda x: f"{x:.2f}")

    exibicao["99"] = moeda_sem_simbolo(dados["ganhos_99"]).where(~folga, "-")
    exibicao["ID / PT"] = moeda_sem_simbolo(dados["ganhos_id"]).where(~folga, "-")
    exibicao["Total Dia"] = moeda_sem_simbolo(dados["Total Dia"]).where(~folga, "-")
    exibicao["Passe"] = moeda_sem_simbolo(dados["custo_passe"]).where(~folga, "-")
    exibicao["Energia"] = moeda_sem_simbolo(dados["Energia"]).where(~folga, "-")
    exibicao["Custo Total"] = moeda_sem_simbolo(dados["Custo Total"]).where(~folga, "-")
    exibicao["Lucro Líquido"] = moeda_sem_simbolo(dados["Lucro Líquido"])
    exibicao.loc[folga, "Lucro Líquido"] = "FOLGA"
    exibicao["Horas"] = dados["horas"].map(lambda x: f"{x:.1f}").where(~folga, "-")
    exibicao["R$ / Hora"] = moeda_sem_simbolo(dados["R$ / Hora"]).where(~folga, "-")

    km_ini = dados["km_ini"].map(lambda x: str(int(x)) if x else "-")
    km_fin = dados["km_fin"].map(lambda x: str(int(x)) if x else "-")
    km_rodados = dados["KM Rodados"].map(lambda x: str(int(x)) if x else "-")

    exibicao["KM Inicial"] = km_ini.where(~folga, "-")
    exibicao["KM Final"] = km_fin.where(~folga, "-")
    exibicao["KM Rodados"] = km_rodados.where(~folga, "-")
    exibicao["R$ / km"] = moeda_sem_simbolo(dados["R$ / km"]).where(~folga, "-")

    exibicao["KW Inicial"] = dados["kw_ini"].map(
        lambda x: f"{x:.1f}" if x else "-"
    ).where(~folga, "-")
    exibicao["KW Final"] = dados["kw_fin"].map(
        lambda x: f"{x:.1f}" if x else "-"
    ).where(~folga, "-")
    exibicao["KW Consumido"] = dados["KW Consumido"].map(
        lambda x: f"{x:.1f}" if x else "-"
    ).where(~folga, "-")

    return exibicao


def colorir_melhor_pior_dia(s):
    """Destaca o melhor dia em verde e o pior em vermelho na coluna de Lucro Líquido."""
    valores = pd.to_numeric(s, errors="coerce")
    
    if valores.dropna().empty:
        return [""] * len(s)
    
    max_val = valores.max()
    min_val = valores.min()
    
    estilos = []
    for val in valores:
        if pd.isna(val):
            estilos.append("")
        elif val == max_val:
            estilos.append("background-color: #1b4d3e; color: #d4edda; font-weight: bold;")
        elif val == min_val:
            estilos.append("background-color: #5c1d24; color: #f8d7da; font-weight: bold;")
        else:
            estilos.append("")
    return estilos


# --- RELATÓRIO PNG ---
def gerar_imagem_relatorio(dataframe, resumo):
    """Gera uma imagem PNG com os indicadores e a tabela."""
    if dataframe is None or dataframe.empty:
        fig, ax = plt.subplots(figsize=(8, 3), dpi=300)
        ax.axis("off")
        fig.patch.set_facecolor("#0e1117")
        ax.set_facecolor("#0e1117")
        ax.text(
            0.5,
            0.5,
            "Nenhum lançamento encontrado",
            horizontalalignment="center",
            verticalalignment="center",
            fontsize=14,
            color="#a3a8b8",
            weight="bold",
            transform=ax.transAxes,
        )
        buffer = io.BytesIO()
        plt.savefig(
            buffer,
            format="png",
            bbox_inches="tight",
            dpi=300,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)
        buffer.seek(0)
        return buffer.getvalue()

    num_linhas = len(dataframe)
    fig_altura = max(9, num_linhas * 0.45 + 5.0)
    fig, ax = plt.subplots(figsize=(24, fig_altura), dpi=300)
    ax.axis("off")

    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#0e1117")

    ax.text(
        0.01,
        0.96,
        "🚗 Controle de Motorista de Aplicativo",
        fontsize=18,
        weight="bold",
        color="white",
        transform=ax.transAxes,
    )
    ax.text(
        0.01,
        0.93,
        "Resumo de corridas, custos e desempenho.",
        fontsize=11,
        color="#a3a8b8",
        transform=ax.transAxes,
    )

    metricas = [
        ("Faturamento Total", formatar_moeda(resumo["faturamento"])),
        ("Líquido", formatar_moeda(resumo["lucro"])),
        ("Passe", formatar_moeda(resumo["passe"])),
        ("Energia", formatar_moeda(resumo["energia"])),
        ("Dias Trabalhados", f'{resumo["dias_trabalhados"]}'),
        ("Dia", formatar_moeda(resumo["lucro_medio_dia"])),
        ("Hora", formatar_moeda(resumo["rs_hora"])),
        ("KM", formatar_moeda(resumo["rs_km"])),
        ("Horas Trabalhadas", f'{resumo["horas"]:.1f}h'),
        ("KM Rodados", f'{int(resumo["km"])} km'),
    ]

    x_pos = 0.01
    y_pos = 0.84
    largura_card = 0.09
    altura_card = 0.065

    for titulo, valor in metricas:
        rect = plt.Rectangle(
            (x_pos, y_pos),
            largura_card,
            altura_card,
            transform=ax.transAxes,
            facecolor="#1e2330",
            edgecolor="#2d3748",
            linewidth=1.5,
        )
        ax.add_patch(rect)
        ax.text(
            x_pos + 0.005,
            y_pos + 0.042,
            titulo,
            fontsize=8.0,
            color="#a3a8b8",
            transform=ax.transAxes,
            weight="bold",
        )
        ax.text(
            x_pos + 0.005,
            y_pos + 0.015,
            valor,
            fontsize=9.5,
            color="white",
            transform=ax.transAxes,
            weight="bold",
        )
        x_pos += largura_card + 0.007

    ax.text(
        0.01,
        0.73,
        "📋 Tabela Consolidada de Lançamentos",
        fontsize=14,
        weight="bold",
        color="white",
        transform=ax.transAxes,
    )

    tabela = ax.table(
        cellText=dataframe.values,
        colLabels=dataframe.columns,
        loc="center",
        cellLoc="center",
        bbox=[0.01, 0.04, 0.98, 0.65],
    )
    tabela.auto_set_font_size(False)
    tabela.set_fontsize(8.5)

    for key, cell in tabela.get_celld().items():
        cell.set_edgecolor("#2d3748")
        if key[0] == 0:
            cell.set_facecolor("#1f77b4")
            cell.set_text_props(weight="bold", color="white")
        else:
            cell.set_facecolor("#161b22" if key[0] % 2 == 0 else "#0e1117")
            cell.set_text_props(color="#e6edf3")

    buffer = io.BytesIO()
    plt.savefig(
        buffer,
        format="png",
        bbox_inches="tight",
        dpi=300,
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


# --- CSS PERSONALIZADO ---
st.markdown(
    """
    <style>
        [data-testid="stSidebar"] {
            min-width: 220px;
            max-width: 220px;
        }
        [data-testid="stDataFrame"] {
            width: 100% !important;
        }
        [data-testid="stDataFrame"] > div {
            width: 100% !important;
            max-height: none !important;
        }
        [data-testid="stDataFrame"] div[data-baseweb="block"] {
            width: 100% !important;
            max-height: none !important;
        }
        [data-testid="stDataFrame"] table {
            width: 100% !important;
        }
        div[data-testid="stDataFrame"] div[tabindex="0"] {
            max-height: none !important;
        }
    </style>
""",
    unsafe_allow_html=True,
)

# --- CONEXÃO COM O BANCO DE DADOS ---
conn = conectar_banco()
inicializar_banco(conn)
fazer_backup_automatico(conn)

# --- ESTADOS DA SESSÃO ---
if "confirmar_apagar_data" not in st.session_state:
    st.session_state.confirmar_apagar_data = False

if "confirmar_apagar_tudo" not in st.session_state:
    st.session_state.confirmar_apagar_tudo = False

if "data_em_edicao" not in st.session_state:
    st.session_state.data_em_edicao = None

# --- CARREGAR DADOS DO BANCO ---
df = carregar_lancamentos(conn)
datas_cadastradas = df["data"].tolist() if not df.empty else []


# --- LATERAL: FILTRO DE MÊS ---
st.sidebar.header("📅 Filtro de Período")
mes_selecionado_nome = "Todos"

hoje_obj = datetime.date.today()
nome_mes_atual = MESES_PT.get(hoje_obj.month)
mes_atual_str = f"{nome_mes_atual} / {hoje_obj.year}"

opcoes_mes = ["Todos", mes_atual_str]

if not df.empty:
    df_temp_anos_meses = preparar_dados(df)
    df_temp_anos_meses["Data_Obj"] = pd.to_datetime(
        df_temp_anos_meses["data"], format="%d/%m/%Y", errors="coerce"
    )
    anos_meses = (
        df_temp_anos_meses["Data_Obj"]
        .dt.to_period("M")
        .dropna()
        .unique()
    )
    anos_meses = sorted(anos_meses, reverse=True)

    for p in anos_meses:
        nome_mes = MESES_PT.get(p.month, str(p.month))
        rotulo_mes = f"{nome_mes} / {p.year}"
        if rotulo_mes not in opcoes_mes:
            opcoes_mes.append(rotulo_mes)

indice_padrao = opcoes_mes.index(mes_atual_str) if mes_atual_str in opcoes_mes else 0

filtro_mes_escolhido = st.sidebar.selectbox(
    "Filtrar por Mês",
    opcoes_mes,
    index=indice_padrao,
    help="Selecione um mês específico ou 'Todos' para ver o histórico completo.",
)
mes_selecionado_nome = filtro_mes_escolhido

st.sidebar.markdown("---")

# --- LATERAL: METAS E CONFIGURAÇÕES ---
st.sidebar.header("🎯 Metas e Configurações")
meta_lucro_semanal = st.sidebar.number_input(
    "Meta de Lucro Semanal (R$)",
    min_value=0.0,
    value=2300.0,
    step=50.0,
    help="Define a sua meta de lucro acumulado para a semana.",
)

st.sidebar.markdown("---")

# --- LATERAL: LANÇAMENTO E EDIÇÃO DIÁRIA ---
st.sidebar.markdown("📝 **Painel Diário**")

reg_atual = None
if st.session_state.data_em_edicao and not df.empty:
    match = df[df["data"] == st.session_state.data_em_edicao]
    if not match.empty:
        reg_atual = match.iloc[0]

editando = reg_atual is not None

if editando:
    st.sidebar.warning(f"✏️ A editar o dia: **{st.session_state.data_em_edicao}**")
    if st.sidebar.button("❌ Cancelar Edição", use_container_width=True):
        st.session_state.data_em_edicao = None
        st.rerun()
    data_selecionada_input = datetime.datetime.strptime(st.session_state.data_em_edicao, "%d/%m/%Y").date()
    data_str_atual = st.session_state.data_em_edicao
    data_ja_existe = False
else:
    data_selecionada_input = st.sidebar.date_input(
        "Data do Registro",
        value=datetime.date.today(),
        max_value=datetime.date.today(),
        format="DD/MM/YYYY",
        help="Escolha a data do novo lançamento.",
    )
    data_str_atual = data_selecionada_input.strftime("%d/%m/%Y") if data_selecionada_input else ""
    data_ja_existe = data_str_atual in datas_cadastradas
    if data_ja_existe:
        st.sidebar.warning(f"⚠️ A data {data_str_atual} já tem registo. Selecione-a na tabela abaixo para a editar.")

val_folga_ini = bool(reg_atual["is_folga"]) if reg_atual is not None else False
val_99_ini = valor_inicial(reg_atual, "ganhos_99")
val_id_ini = valor_inicial(reg_atual, "ganhos_id")
val_passe_ini = valor_inicial(reg_atual, "custo_passe")
val_horas_ini = valor_inicial(reg_atual, "horas")
val_km_ini_ini = valor_inicial(reg_atual, "km_ini")
val_km_fin_ini = valor_inicial(reg_atual, "km_fin")
val_kw_ini_ini = valor_inicial(reg_atual, "kw_ini")
val_kw_fin_ini = valor_inicial(reg_atual, "kw_fin")
val_preco_kw_ini = valor_inicial(reg_atual, "preco_kw")

is_folga = st.sidebar.checkbox(
    "🌴 Marcar este dia como FOLGA",
    value=val_folga_ini,
    key="input_folga",
)

if not is_folga:
    with st.sidebar.expander("💰 Ganhos e custos", expanded=True):
        ganhos_99 = (
            st.number_input(
                "99",
                min_value=0.0,
                value=val_99_ini,
                step=10.0,
                format="%.2f",
                placeholder="0,00",
                key="input_99",
            )
            or 0.0
        )
        ganhos_id = (
            st.number_input(
                "ID / PT",
                min_value=0.0,
                value=val_id_ini,
                step=10.0,
                format="%.2f",
                placeholder="0,00",
                key="input_id",
            )
            or 0.0
        )
        custo_passe = (
            st.number_input(
                "Passe",
                min_value=0.0,
                value=val_passe_ini,
                step=5.0,
                format="%.2f",
                placeholder="0,00",
                key="input_passe",
            )
            or 0.0
        )

    with st.sidebar.expander("⏱️ Jornada e quilometragem", expanded=True):
        horas = (
            st.number_input(
                "Horas Trabalhadas",
                min_value=0.0,
                value=val_horas_ini,
                step=0.5,
                format="%.2f",
                placeholder="0,00",
                key="input_horas",
            )
            or 0.0
        )
        km_ini = (
            st.number_input(
                "KM Inicial",
                min_value=0.0,
                value=val_km_ini_ini,
                step=1.0,
                format="%.2f",
                placeholder="0,00",
                key="input_km_ini",
            )
            or 0.0
        )
        km_fin = (
            st.number_input(
                "KM Final",
                min_value=0.0,
                value=val_km_fin_ini,
                step=1.0,
                format="%.2f",
                placeholder="0,00",
                key="input_km_fin",
            )
            or 0.0
        )

    with st.sidebar.expander("⚡ Energia", expanded=True):
        kw_ini = (
            st.number_input(
                "KW Inicial",
                min_value=0.0,
                value=val_kw_ini_ini,
                step=1.0,
                format="%.2f",
                placeholder="0,00",
                key="input_kw_ini",
            )
            or 0.0
        )
        kw_fin = (
            st.number_input(
                "KW Final",
                min_value=0.0,
                value=val_kw_fin_ini,
                step=1.0,
                format="%.2f",
                placeholder="0,00",
                key="input_kw_fin",
            )
            or 0.0
        )
        preco_kw = (
            st.number_input(
                "Preço por KW",
                min_value=0.0,
                value=val_preco_kw_ini,
                step=0.01,
                format="%.2f",
                placeholder="0,00",
                key="input_preco_kw",
            )
            or 0.0
        )
else:
    ganhos_99 = ganhos_id = custo_passe = 0.0
    horas = km_ini = km_fin = kw_ini = kw_fin = preco_kw = 0.0

erro_medicao = None
if not is_folga:
    if km_ini > 0 and km_fin > 0 and km_fin < km_ini:
        erro_medicao = "O KM Final não pode ser menor que o KM Inicial."
    elif kw_ini > 0 and kw_fin > 0 and kw_fin < kw_ini:
        erro_medicao = "O KW Final não pode ser menor que o KW Inicial."

if erro_medicao:
    st.sidebar.error(f"⚠️ {erro_medicao}")

desativar_botao = (
    data_selecionada_input is None
    or (data_ja_existe and not editando)
    or erro_medicao is not None
)

texto_botao = "💾 Atualizar Registo" if editando else "💾 Salvar Novo Dia"
botao_salvar = st.sidebar.button(
    texto_botao,
    use_container_width=True,
    disabled=desativar_botao,
)

if botao_salvar and not desativar_botao:
    flag_folga = 1 if is_folga else 0
    dia_ingles = data_selecionada_input.strftime("%a")
    dia_semana = DIAS_PT.get(dia_ingles, dia_ingles.upper())

    dados_lancamento = {
        "data": data_str_atual,
        "dia_semana": dia_semana,
        "is_folga": flag_folga,
        "ganhos_99": ganhos_99,
        "ganhos_id": ganhos_id,
        "custo_passe": custo_passe,
        "horas": horas,
        "km_ini": km_ini,
        "km_fin": km_fin,
        "kw_ini": kw_ini,
        "kw_fin": kw_fin,
        "preco_kw": preco_kw,
    }

    salvar_lancamento(conn, dados_lancamento)
    st.session_state.data_em_edicao = None
    st.sidebar.success(f"Registo de {data_str_atual} guardado/atualizado com sucesso!")
    st.rerun()


# --- LATERAL: GERENCIAMENTO E EXCLUSÃO ---
if not df.empty:
    st.sidebar.markdown("---")
    st.sidebar.header("⚙ Gerenciar Registros")

    datas_disponiveis = sorted(
        df["data"].tolist(),
        key=lambda data: pd.to_datetime(data, format="%d/%m/%Y", errors="coerce"),
        reverse=True,
    )
    data_para_apagar = st.sidebar.selectbox(
        "Escolha a data para apagar",
        datas_disponiveis,
        help="Esta ação remove somente o registo escolhido.",
    )

    if st.sidebar.button("🗑 Apagar Data Selecionada", use_container_width=True):
        st.session_state.confirmar_apagar_data = True
        st.session_state.confirmar_apagar_tudo = False

    if st.session_state.confirmar_apagar_data:
        st.sidebar.warning(
            f"Tem a certeza de que deseja apagar o registo de **{data_para_apagar}**?"
        )
        col_a, col_b = st.sidebar.columns(2)

        with col_a:
            if st.button("Sim, apagar", key="confirmar_apagar_data_btn"):
                apagar_lancamento(conn, data_para_apagar)
                st.session_state.confirmar_apagar_data = False
                if st.session_state.data_em_edicao == data_para_apagar:
                    st.session_state.data_em_edicao = None
                st.rerun()

        with col_b:
            if st.button("Cancelar", key="cancelar_apagar_data_btn"):
                st.session_state.confirmar_apagar_data = False
                st.rerun()

    st.sidebar.markdown("---")
    if st.sidebar.button(
        "⚠️ Apagar TUDO do Banco",
        use_container_width=True,
    ):
        st.session_state.confirmar_apagar_tudo = True
        st.session_state.confirmar_apagar_data = False

    if st.session_state.confirmar_apagar_tudo:
        st.sidebar.error(
            "⚠️ Esta ação apagará **todos os registos** e não pode ser desfeita."
        )
        col_c, col_d = st.sidebar.columns(2)

        with col_c:
            if st.button("Sim, apagar tudo", key="confirmar_apagar_tudo_btn"):
                apagar_todos_lancamentos(conn)
                st.session_state.confirmar_apagar_tudo = False
                st.session_state.data_em_edicao = None
                st.rerun()

        with col_d:
            if st.button("Cancelar", key="cancelar_apagar_tudo_btn"):
                st.session_state.confirmar_apagar_tudo = False
                st.rerun()


# --- LATERAL: BACKUP MANUAL ---
st.sidebar.markdown("---")
st.sidebar.header("💾 Backup")
st.sidebar.caption("O backup automático acontece a cada 30 minutos.")

if st.sidebar.button("💾 Fazer Backup Agora", use_container_width=True):
    try:
        destinos = salvar_backup_arquivos(conn)
        st.sidebar.success(f"Backup concluído ({len(destinos)} ficheiro(s)).")
    except RuntimeError as erro:
        st.sidebar.error(str(erro))


# --- TÍTULO PRINCIPAL E CABEÇALHO ---
st.markdown("## 🚗 Controle de Motorista de Aplicativo")
st.caption(
    "Controle corridas, custos, horas, quilometragem e desempenho num só lugar."
)

# --- PAINEL E CÁLCULOS ---
if df.empty:
    st.info(
        "ℹ️ Nenhum registo encontrado. Use o formulário da barra lateral "
        "para cadastrar o seu primeiro dia de trabalho ou folga."
    )
else:
    df_calc = calcular_indicadores(df)

    df_calc["Data_Obj"] = pd.to_datetime(
        df_calc["data"], format="%d/%m/%Y", errors="coerce"
    )
    df_calc = df_calc.sort_values("Data_Obj", ascending=True)

    # --- APLICAR FILTRO DE MÊS SELECIONADO ---
    if mes_selecionado_nome != "Todos":
        partes = mes_selecionado_nome.split(" / ")
        nome_m_str = partes[0]
        ano_str = partes[1]
        
        mes_num_inv = {v: k for k, v in MESES_PT.items()}
        num_m = mes_num_inv.get(nome_m_str)
        ano_i = int(ano_str)

        df_filtrado = df_calc[
            (df_calc["Data_Obj"].dt.month == num_m) & 
            (df_calc["Data_Obj"].dt.year == ano_i)
        ].copy()
    else:
        df_filtrado = df_calc.copy()

    resumo = calcular_resumo(df_filtrado)

    hoje = datetime.date.today()
    inicio_semana = hoje - datetime.timedelta(days=hoje.weekday())
    fim_semana = inicio_semana + datetime.timedelta(days=6)

    semana = df_calc[
        (df_calc["Data_Obj"].dt.date >= inicio_semana)
        & (df_calc["Data_Obj"].dt.date <= fim_semana)
        & (df_calc["is_folga"] == 0)
    ]
    lucro_semana = semana["Lucro Líquido"].sum()
    faturamento_semana = semana["Total Dia"].sum()

    # --- META SEMANAL ---
    st.markdown("### 🎯 Meta da Semana")
    meta_col1, meta_col2, meta_col3 = st.columns(3)
    meta_col1.metric("Meta de Lucro", formatar_moeda(meta_lucro_semanal))
    meta_col2.metric("Lucro Atual", formatar_moeda(lucro_semana))
    meta_col3.metric("Faturamento da Semana", formatar_moeda(faturamento_semana))

    if meta_lucro_semanal > 0:
        progresso = max(0.0, min(lucro_semana / meta_lucro_semanal, 1.0))
        st.progress(progresso)
        if lucro_semana >= meta_lucro_semanal:
            st.success("🎉 Meta semanal atingida!")
        else:
            falta = meta_lucro_semanal - lucro_semana
            st.caption(f"Faltam {formatar_moeda(falta)} para atingir a meta.")
    else:
        st.caption("Defina uma meta semanal maior que zero na barra lateral.")

    st.markdown("---")

    # --- INDICADORES GERAIS ---
    if mes_selecionado_nome == "Todos":
        titulo_visao = "📊 Visão Geral"
    else:
        titulo_visao = f"📊 {mes_selecionado_nome}"

    st.markdown(f"### {titulo_visao}")
     
    col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
    col_m1.metric("Faturamento Total", formatar_moeda(resumo["faturamento"]))
    col_m2.metric("Líquido", formatar_moeda(resumo["lucro"]))
    col_m3.metric("Passe", formatar_moeda(resumo["passe"]))
    col_m4.metric("Energia", formatar_moeda(resumo["energia"]))
    col_m5.metric("Dias Trabalhados", resumo["dias_trabalhados"])

    col_m6, col_m7, col_m8, col_m9, col_m10 = st.columns(5)
    col_m6.metric("Dia", formatar_moeda(resumo["lucro_medio_dia"]))
    col_m7.metric("Hora", formatar_moeda(resumo["rs_hora"]))
    col_m8.metric("KM", formatar_moeda(resumo["rs_km"]))
    col_m9.metric("Horas Trabalhadas", f'{resumo["horas"]:.1f} h')
    col_m10.metric("KM Rodados", f'{int(resumo["km"])} km')

    st.markdown("---")

    # --- TABELA COM DESTAQUE (MELHOR DIA = VERDE, PIOR DIA = VERMELHO) ---
    st.markdown("### 📋 Lançamentos")
    st.caption("Selecione uma linha na tabela com a caixa de marcação à esquerda para carregar os dados para edição.")

    if df_filtrado.empty:
        st.info("ℹ️ Nenhum lançamento encontrado para o período selecionado.")
        imagem_bytes = gerar_imagem_relatorio(pd.DataFrame(), resumo)
        csv_bytes = b""
    else:
        df_exibicao = preparar_tabela_exibicao(df_filtrado)
        indices_ordenados = df_filtrado.index
        df_exibicao_ordenada = df_exibicao.loc[indices_ordenados]

        imagem_bytes = gerar_imagem_relatorio(df_exibicao_ordenada, resumo)
        csv_bytes = df_exibicao_ordenada.to_csv(
            index=False,
            sep=";",
            decimal=",",
        ).encode("utf-8-sig")

        df_estilizado = df_exibicao_ordenada.style.apply(
            colorir_melhor_pior_dia, subset=["Lucro Líquido"]
        )

        evento_tabela = st.dataframe(
            df_estilizado,
            use_container_width=True,
            height="content",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )

        if evento_tabela.selection and evento_tabela.selection.rows:
            linha_idx = evento_tabela.selection.rows[0]
            data_selecionada_tabela = df_exibicao_ordenada.iloc[linha_idx]["Data"]
            if st.session_state.data_em_edicao != data_selecionada_tabela:
                st.session_state.data_em_edicao = data_selecionada_tabela
                st.rerun()

        st.caption(
            f"Período exibido: {df_filtrado['data'].iloc[0]} até "
            f"{df_filtrado['data'].iloc[-1]} • "
            f"{len(df_filtrado)} registo(s)"
        )

    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 4])
    with btn_col1:
        st.download_button(
            "📥 Baixar PNG",
            data=imagem_bytes,
            file_name=f"relatorio_motorista_{mes_selecionado_nome.lower().replace('/', '_').replace(' ', '')}.png",
            mime="image/png",
            use_container_width=True,
        )
    with btn_col2:
        st.download_button(
            "📄 Baixar CSV",
            data=csv_bytes,
            file_name=f"relatorio_motorista_{mes_selecionado_nome.lower().replace('/', '_').replace(' ', '')}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=df_filtrado.empty,
        )

    # --- RESUMO POR SEMANA ---
    st.markdown("---")
    st.markdown("### 📅 Resumo por Semana (Segunda a Domingo)")

    if not df_filtrado.empty:
        df_semanas = df_filtrado.copy()
        df_semanas["Semana_Inicio"] = df_semanas["Data_Obj"].dt.to_period("W-SUN").dt.start_time
        df_semanas["Semana_Fim"] = df_semanas["Data_Obj"].dt.to_period("W-SUN").dt.end_time

        grupos_semana = df_semanas.groupby(["Semana_Inicio", "Semana_Fim"])

        dados_tabela_semanas = []

        for (s_inicio, s_fim), g in grupos_semana:
            fat = g["Total Dia"].sum()
            lucro = g["Lucro Líquido"].sum()
            horas = g["horas"].sum()
            km = g["KM Rodados"].sum()
            dias_trab = int((g["is_folga"] == 0).sum())
             
            rs_km = ((g["Total Dia"] - g["custo_passe"]).sum() / km) if km > 0 else 0.0
            rs_hora = (lucro / horas) if horas > 0 else 0.0
             
            rotulo_periodo = f"{s_inicio.strftime('%d/%m/%Y')} a {s_fim.strftime('%d/%m/%Y')}"
             
            dados_tabela_semanas.append({
                "Período": rotulo_periodo,
                "Dias Trab.": dias_trab,
                "Faturamento": formatar_moeda(fat),
                "Lucro Líquido": formatar_moeda(lucro),
                "Horas": f"{horas:.1f}",
                "KM Rodados": f"{int(km)}",
                "R$ / Hora": formatar_moeda(rs_hora),
                "R$ / KM": formatar_moeda(rs_km),
            })

        if dados_tabela_semanas:
            df_resumo_semanas = pd.DataFrame(dados_tabela_semanas)
            st.dataframe(
                df_resumo_semanas,
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info("Sem dados para exibir no resumo semanal neste período.")