import os
import sqlite3
import struct
import math
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="API Hotéis de Trânsito - Forças Armadas")

# Habilitar CORS para facilitar requisições locais se necessário
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CAMINHO_GPKG = os.path.join(os.path.dirname(__file__), "Hoteis_de_Transito_FA.gpkg")


def extrair_ponto_geopackage(blob: bytes) -> tuple[Optional[float], Optional[float]]:
    """
    Decodifica um blob de geometria GeoPackage (GPKG Binary Standard)
    para extrair longitude (X) e latitude (Y) sem bibliotecas externas.
    """
    if not blob or len(blob) < 8:
        return None, None

    # O cabeçalho GeoPackage inicia com os bytes mágicos 'GP' (0x47, 0x50)
    if blob[0:2] != b"GP":
        return None, None

    flags = blob[3]
    indicador_envelope = (flags >> 1) & 0x07

    # Tamanho do envelope conforme especificação OGC GeoPackage:
    # 0 = sem envelope (0 bytes)
    # 1 = envelope 2D (32 bytes)
    # 2 = envelope 3D com Z (48 bytes)
    # 3 = envelope 3D com M (48 bytes)
    # 4 = envelope 4D com Z e M (64 bytes)
    tamanhos_envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    tamanho_envelope = tamanhos_envelope.get(indicador_envelope, 0)
    offset_wkb = 8 + tamanho_envelope

    if len(blob) < offset_wkb + 21:
        return None, None

    # Leitura do WKB (Well-Known Binary)
    byte_order = blob[offset_wkb]
    ordem_endian = "<" if byte_order == 1 else ">"

    geom_type = struct.unpack(f"{ordem_endian}I", blob[offset_wkb + 1 : offset_wkb + 5])[0]

    # Geometrias de ponto (Point = 1, PointZ = 1001, etc.)
    if geom_type % 1000 == 1:
        x, y = struct.unpack(f"{ordem_endian}dd", blob[offset_wkb + 5 : offset_wkb + 21])
        return x, y

    return None, None


def calcular_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calcula a distância do grande círculo entre dois pontos (em km)
    utilizando a fórmula de Haversine com funções nativas do módulo math.
    """
    raio_terra_km = 6371.0

    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)

    rad_lat1 = math.radians(lat1)
    rad_lat2 = math.radians(lat2)

    a = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(rad_lat1) * math.cos(rad_lat2) * (math.sin(d_lon / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return raio_terra_km * c


def carregar_dados_hoteis() -> List[Dict[str, Any]]:
    """
    Lê o arquivo GeoPackage usando apenas sqlite3, descobrindo dinamicamente
    a tabela de feições e extraindo os atributos e coordenadas de cada registro.
    """
    if not os.path.exists(CAMINHO_GPKG):
        raise HTTPException(
            status_code=404,
            detail=f"Arquivo de banco de dados não encontrado: {os.path.basename(CAMINHO_GPKG)}",
        )

    conexao = sqlite3.connect(CAMINHO_GPKG)
    conexao.row_factory = sqlite3.Row
    cursor = conexao.cursor()

    try:
        # Localiza a tabela de feições registrada no GeoPackage
        cursor.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features' LIMIT 1")
        linha_conteudo = cursor.fetchone()

        if linha_conteudo:
            nome_tabela = linha_conteudo["table_name"]
        else:
            # Fallback caso gpkg_contents não esteja presente: busca primeira tabela que não seja do sistema
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'gpkg_%' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            )
            tabela_fallback = cursor.fetchone()
            if not tabela_fallback:
                return []
            nome_tabela = tabela_fallback["name"]

        cursor.execute(f"SELECT * FROM \"{nome_tabela}\"")
        linhas = cursor.fetchall()

        registros = []
        for linha in linhas:
            dados = dict(linha)
            lon = None
            lat = None

            # 1. Tenta identificar latitude/longitude em colunas alfanuméricas comuns
            nomes_colunas = {k.lower(): k for k in dados.keys()}
            for col_lat in ["lat", "latitude", "y"]:
                if col_lat in nomes_colunas and dados[nomes_colunas[col_lat]] is not None:
                    try:
                        lat = float(dados[nomes_colunas[col_lat]])
                        break
                    except (ValueError, TypeError):
                        pass

            for col_lon in ["lon", "lng", "longitude", "long", "x"]:
                if col_lon in nomes_colunas and dados[nomes_colunas[col_lon]] is not None:
                    try:
                        lon = float(dados[nomes_colunas[col_lon]])
                        break
                    except (ValueError, TypeError):
                        pass

            # 2. Se não encontrou nas colunas de texto/número, extrai da coluna de geometria GPKG
            if lat is None or lon is None:
                for chave, valor in list(dados.items()):
                    if isinstance(valor, bytes):
                        x_geo, y_geo = extrair_ponto_geopackage(valor)
                        if x_geo is not None and y_geo is not None:
                            lon, lat = x_geo, y_geo
                            break

            # Remove colunas binárias antes de retornar o JSON para evitar erro de serialização
            dados_limpos = {
                k: v for k, v in dados.items() if not isinstance(v, bytes)
            }

            dados_limpos["latitude"] = lat
            dados_limpos["longitude"] = lon

            registros.append(dados_limpos)

        return registros
    finally:
        conexao.close()


# Cache global em memória carregado na inicialização do servidor
CACHE_HOTEIS: List[Dict[str, Any]] = []


@app.on_event("startup")
def carregar_cache_inicial():
    """Carrega os dados do GeoPackage em memória uma única vez ao iniciar o FastAPI."""
    global CACHE_HOTEIS
    CACHE_HOTEIS = carregar_dados_hoteis()


@app.get("/ping")
def ping():
    """Rota leve de health check para evitar spin-down do Render."""
    return {"status": "ok"}


@app.get("/api/hoteis")
def listar_hoteis(
    lat: Optional[float] = Query(None, description="Latitude do ponto de referência"),
    lon: Optional[float] = Query(None, description="Longitude do ponto de referência"),
    raio_km: Optional[float] = Query(100.0, description="Raio de busca em quilômetros"),
    busca: Optional[str] = Query(None, description="Termo textual para filtro (nome, município, etc.)"),
):
    """
    Retorna a lista de hotéis de trânsito a partir do cache em memória.
    Caso latitude e longitude sejam fornecidas, calcula a distância via Haversine
    e filtra pelo raio informado (em km).
    """
    global CACHE_HOTEIS
    if not CACHE_HOTEIS:
        CACHE_HOTEIS = carregar_dados_hoteis()

    todos_hoteis = CACHE_HOTEIS
    resultados = []

    termo_busca = busca.strip().lower() if busca else None

    for hotel in todos_hoteis:
        # Filtro textual genérico (se informado)
        if termo_busca:
            conteudo_texto = " ".join(
                str(v).lower() for v in hotel.values() if v is not None
            )
            if termo_busca not in conteudo_texto:
                continue

        h_lat = hotel.get("latitude")
        h_lon = hotel.get("longitude")

        # Filtro e cálculo de distância Haversine
        if lat is not None and lon is not None:
            if h_lat is not None and h_lon is not None:
                distancia = calcular_haversine(lat, lon, h_lat, h_lon)
                if raio_km is None or distancia <= raio_km:
                    hotel_com_distancia = dict(hotel)
                    hotel_com_distancia["distancia_km"] = round(distancia, 2)
                    resultados.append(hotel_com_distancia)
        else:
            resultados.append(hotel)

    # Se houve cálculo de distância, ordenar pelo hotel mais próximo
    if lat is not None and lon is not None:
        resultados.sort(key=lambda item: item.get("distancia_km", float("inf")))

    return {
        "total": len(resultados),
        "centro": {"lat": lat, "lon": lon} if lat is not None and lon is not None else None,
        "raio_km": raio_km if lat is not None and lon is not None else None,
        "hoteis": resultados,
    }


@app.get("/api/expand-url")
def expandir_url(url: str = Query(..., description="URL curta do Google Maps ou Waze para expandir")):
    """
    Segue redirecionamentos (follow redirects) de links curtos (maps.app.goo.gl, goo.gl, waze.com)
    usando apenas urllib nativo e retorna a URL completa final para contornar bloqueios de CORS.
    """
    url_limpa = (url or "").strip()
    if not url_limpa:
        raise HTTPException(status_code=400, detail="Parâmetro 'url' é obrigatório.")

    parsed = urllib.parse.urlparse(url_limpa)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL inválida. Utilize http:// ou https://.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    req = urllib.request.Request(url_limpa, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return {"full_url": response.geturl()}
    except urllib.error.HTTPError as e:
        url_final = e.geturl()
        if url_final and url_final != url_limpa:
            return {"full_url": url_final}
        raise HTTPException(status_code=400, detail=f"Não foi possível expandir o link (HTTP {e.code}).")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao expandir o link: {str(e)}")


# Monta a pasta static/ na raiz / para servir a interface web
caminho_static = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(caminho_static):
    os.makedirs(caminho_static, exist_ok=True)

app.mount("/", StaticFiles(directory=caminho_static, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
import os
import sqlite3
import struct
import math
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="API Hotéis de Trânsito - Forças Armadas")

# Habilitar CORS para facilitar requisições locais se necessário
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CAMINHO_GPKG = os.path.join(os.path.dirname(__file__), "Hoteis_de_Transito_FA.gpkg")


def extrair_ponto_geopackage(blob: bytes) -> tuple[Optional[float], Optional[float]]:
    """
    Decodifica um blob de geometria GeoPackage (GPKG Binary Standard)
    para extrair longitude (X) e latitude (Y) sem bibliotecas externas.
    """
    if not blob or len(blob) < 8:
        return None, None

    # O cabeçalho GeoPackage inicia com os bytes mágicos 'GP' (0x47, 0x50)
    if blob[0:2] != b"GP":
        return None, None

    flags = blob[3]
    indicador_envelope = (flags >> 1) & 0x07

    # Tamanho do envelope conforme especificação OGC GeoPackage:
    # 0 = sem envelope (0 bytes)
    # 1 = envelope 2D (32 bytes)
    # 2 = envelope 3D com Z (48 bytes)
    # 3 = envelope 3D com M (48 bytes)
    # 4 = envelope 4D com Z e M (64 bytes)
    tamanhos_envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    tamanho_envelope = tamanhos_envelope.get(indicador_envelope, 0)
    offset_wkb = 8 + tamanho_envelope

    if len(blob) < offset_wkb + 21:
        return None, None

    # Leitura do WKB (Well-Known Binary)
    byte_order = blob[offset_wkb]
    ordem_endian = "<" if byte_order == 1 else ">"

    geom_type = struct.unpack(f"{ordem_endian}I", blob[offset_wkb + 1 : offset_wkb + 5])[0]

    # Geometrias de ponto (Point = 1, PointZ = 1001, etc.)
    if geom_type % 1000 == 1:
        x, y = struct.unpack(f"{ordem_endian}dd", blob[offset_wkb + 5 : offset_wkb + 21])
        return x, y

    return None, None


def calcular_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calcula a distância do grande círculo entre dois pontos (em km)
    utilizando a fórmula de Haversine com funções nativas do módulo math.
    """
    raio_terra_km = 6371.0

    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)

    rad_lat1 = math.radians(lat1)
    rad_lat2 = math.radians(lat2)

    a = (
        math.sin(d_lat / 2.0) ** 2
        + math.cos(rad_lat1) * math.cos(rad_lat2) * (math.sin(d_lon / 2.0) ** 2)
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return raio_terra_km * c


def carregar_dados_hoteis() -> List[Dict[str, Any]]:
    """
    Lê o arquivo GeoPackage usando apenas sqlite3, descobrindo dinamicamente
    a tabela de feições e extraindo os atributos e coordenadas de cada registro.
    """
    if not os.path.exists(CAMINHO_GPKG):
        raise HTTPException(
            status_code=404,
            detail=f"Arquivo de banco de dados não encontrado: {os.path.basename(CAMINHO_GPKG)}",
        )

    conexao = sqlite3.connect(CAMINHO_GPKG)
    conexao.row_factory = sqlite3.Row
    cursor = conexao.cursor()

    try:
        # Localiza a tabela de feições registrada no GeoPackage
        cursor.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features' LIMIT 1")
        linha_conteudo = cursor.fetchone()

        if linha_conteudo:
            nome_tabela = linha_conteudo["table_name"]
        else:
            # Fallback caso gpkg_contents não esteja presente: busca primeira tabela que não seja do sistema
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'gpkg_%' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            )
            tabela_fallback = cursor.fetchone()
            if not tabela_fallback:
                return []
            nome_tabela = tabela_fallback["name"]

        cursor.execute(f"SELECT * FROM \"{nome_tabela}\"")
        linhas = cursor.fetchall()

        registros = []
        for linha in linhas:
            dados = dict(linha)
            lon = None
            lat = None

            # 1. Tenta identificar latitude/longitude em colunas alfanuméricas comuns
            nomes_colunas = {k.lower(): k for k in dados.keys()}
            for col_lat in ["lat", "latitude", "y"]:
                if col_lat in nomes_colunas and dados[nomes_colunas[col_lat]] is not None:
                    try:
                        lat = float(dados[nomes_colunas[col_lat]])
                        break
                    except (ValueError, TypeError):
                        pass

            for col_lon in ["lon", "lng", "longitude", "long", "x"]:
                if col_lon in nomes_colunas and dados[nomes_colunas[col_lon]] is not None:
                    try:
                        lon = float(dados[nomes_colunas[col_lon]])
                        break
                    except (ValueError, TypeError):
                        pass

            # 2. Se não encontrou nas colunas de texto/número, extrai da coluna de geometria GPKG
            if lat is None or lon is None:
                for chave, valor in list(dados.items()):
                    if isinstance(valor, bytes):
                        x_geo, y_geo = extrair_ponto_geopackage(valor)
                        if x_geo is not None and y_geo is not None:
                            lon, lat = x_geo, y_geo
                            break

            # Remove colunas binárias antes de retornar o JSON para evitar erro de serialização
            dados_limpos = {
                k: v for k, v in dados.items() if not isinstance(v, bytes)
            }

            dados_limpos["latitude"] = lat
            dados_limpos["longitude"] = lon

            registros.append(dados_limpos)

        return registros
    finally:
        conexao.close()


@app.get("/api/hoteis")
def listar_hoteis(
    lat: Optional[float] = Query(None, description="Latitude do ponto de referência"),
    lon: Optional[float] = Query(None, description="Longitude do ponto de referência"),
    raio_km: Optional[float] = Query(100.0, description="Raio de busca em quilômetros"),
    busca: Optional[str] = Query(None, description="Termo textual para filtro (nome, município, etc.)"),
):
    """
    Retorna a lista de hotéis de trânsito. Caso latitude e longitude sejam fornecidas,
    calcula a distância via Haversine e filtra pelo raio informado (em km).
    """
    todos_hoteis = carregar_dados_hoteis()
    resultados = []

    termo_busca = busca.strip().lower() if busca else None

    for hotel in todos_hoteis:
        # Filtro textual genérico (se informado)
        if termo_busca:
            conteudo_texto = " ".join(
                str(v).lower() for v in hotel.values() if v is not None
            )
            if termo_busca not in conteudo_texto:
                continue

        h_lat = hotel.get("latitude")
        h_lon = hotel.get("longitude")

        # Filtro e cálculo de distância Haversine
        if lat is not None and lon is not None:
            if h_lat is not None and h_lon is not None:
                distancia = calcular_haversine(lat, lon, h_lat, h_lon)
                if raio_km is None or distancia <= raio_km:
                    hotel_com_distancia = dict(hotel)
                    hotel_com_distancia["distancia_km"] = round(distancia, 2)
                    resultados.append(hotel_com_distancia)
        else:
            resultados.append(hotel)

    # Se houve cálculo de distância, ordenar pelo hotel mais próximo
    if lat is not None and lon is not None:
        resultados.sort(key=lambda item: item.get("distancia_km", float("inf")))

    return {
        "total": len(resultados),
        "centro": {"lat": lat, "lon": lon} if lat is not None and lon is not None else None,
        "raio_km": raio_km if lat is not None and lon is not None else None,
        "hoteis": resultados,
    }


# Monta a pasta static/ na raiz / para servir a interface web
caminho_static = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(caminho_static):
    os.makedirs(caminho_static, exist_ok=True)

app.mount("/", StaticFiles(directory=caminho_static, html=True), name="static")
