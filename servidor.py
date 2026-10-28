# ============================================================
#  Audiolivros — servidor web (roda no Hugging Face Spaces)
#  Reaproveita o mesmo código do notebook do Colab (nucleo.py + motor.py)
# ============================================================
import os, io, re, sys, json, time, uuid, hashlib, asyncio, threading, traceback, queue, shutil
from starlette.applications import Starlette
from starlette.responses import JSONResponse, FileResponse, HTMLResponse, Response
from starlette.routing import Route
from starlette.requests import Request

import nucleo
import motor

AQUI = os.path.dirname(os.path.abspath(__file__))
DADOS = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else os.environ.get("PASTA_DADOS", "/tmp/audiolivros")
PASTA_LIVROS = os.path.join(DADOS, "livros")
PASTA_AUDIOS = os.path.join(DADOS, "audios")
for p in (PASTA_LIVROS, PASTA_AUDIOS):
    os.makedirs(p, exist_ok=True)
motor.PASTA_AUDIOS = PASTA_AUDIOS
motor.PASTA_DOWNLOAD = PASTA_LIVROS

SENHA = os.environ.get("APP_SENHA", "").strip()
VOZES_GEMINI = ["Kore", "Charon", "Aoede", "Orus", "Leda", "Puck", "Zephyr", "Fenrir", "Sulafat", "Achird",
                "Gacrux", "Schedar", "Iapetus", "Algieba", "Despina", "Erinome", "Umbriel", "Callirrhoe",
                "Autonoe", "Enceladus", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam",
                "Pulcherrima", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager"]


# ---------------- saída de texto por tarefa ----------------
class _SaidaPorThread(io.TextIOBase):
    """Manda o print() de cada tarefa para o registro dela; o resto vai para o terminal normal."""
    def __init__(self, real):
        self.real, self.destinos = real, {}

    def write(self, s):
        d = self.destinos.get(threading.get_ident())
        if d is not None:
            d(s)
        else:
            self.real.write(s)
        return len(s)

    def flush(self):
        self.real.flush()

SAIDA = _SaidaPorThread(sys.stdout)
sys.stdout = SAIDA


# ---------------- livros ----------------
def _meta_livros():
    caminho = os.path.join(PASTA_LIVROS, "livros.json")
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _salvar_meta(m):
    with open(os.path.join(PASTA_LIVROS, "livros.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)

_CACHE_SUMARIO = {}

def _sumario(caminho):
    if caminho in _CACHE_SUMARIO:
        return _CACHE_SUMARIO[caminho]
    livro = nucleo.LivroPDF(caminho)
    info = {"paginas": livro.n, "tem_texto": livro.tem_texto(), "sumario": []}
    if info["tem_texto"]:
        itens = livro.sumario(detectar_se_vazio=livro.n <= 300)
        if itens:
            nivel_max = min(it["nivel"] for it in itens) + 1
            info["sumario"] = [{"n": k, "titulo": it["titulo"], "pagina": it["pagina"] + 1,
                                "nivel": it["nivel"], "principal": it["nivel"] <= nivel_max}
                               for k, it in enumerate(itens, 1)]
    livro.pdf.close()
    _CACHE_SUMARIO[caminho] = info
    return info

def _registrar_livro(caminho_tmp, nome):
    h = hashlib.md5()
    with open(caminho_tmp, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    lid = h.hexdigest()[:12]
    nome = os.path.basename(nome) or "livro.pdf"
    destino_dir = os.path.join(PASTA_LIVROS, lid)
    os.makedirs(destino_dir, exist_ok=True)
    destino = os.path.join(destino_dir, nome)
    if not os.path.exists(destino):
        shutil.move(caminho_tmp, destino)
    elif os.path.exists(caminho_tmp):
        os.remove(caminho_tmp)
    meta = _meta_livros()
    meta[lid] = {"id": lid, "nome": nome, "caminho": destino, "quando": time.time()}
    _salvar_meta(meta)
    info = _sumario(destino) if nome.lower().endswith(".pdf") else {"paginas": 0, "tem_texto": True, "sumario": []}
    return {**meta[lid], **info, "tipo": "txt" if nome.lower().endswith(".txt") else "pdf"}


# ---------------- fila de geração ----------------
TAREFAS = {}
FILA = queue.Queue()

def _trabalhador():
    while True:
        tid = FILA.get()
        t = TAREFAS.get(tid)
        if not t or t["estado"] == "cancelada":
            continue
        t["estado"], t["inicio"] = "gerando", time.time()
        buf = []

        def anotar(s, t=t, buf=buf):
            buf.append(s)
            texto = "".join(buf)
            linhas = [l.strip() for l in re.split(r"[\r\n]+", texto) if l.strip()]
            t["registro"] = linhas[-12:]
            m = re.findall(r"parte (\d+)/(\d+)", texto)
            if m:
                k, n = map(int, m[-1])
                t["progresso"] = k / n
            m2 = re.findall(r"Gerando (\d+) parte", texto)
            if m2:
                t["partes"] = int(m2[-1])

        SAIDA.destinos[threading.get_ident()] = anotar
        try:
            p = t["pedido"]
            motor.carregar(p["caminho"])
            res = asyncio.run(motor.gerar(
                capitulo=p.get("capitulo", ""), paginas=p.get("paginas", ""),
                motor="Gemini" if p.get("motor") == "gemini" else "Edge",
                voz_edge=p.get("voz_edge") or "pt-BR-FranciscaNeural",
                velocidade_edge=int(p.get("velocidade_edge") or 0),
                voz_gemini=p.get("voz_gemini") or "Kore",
                estilo_gemini=p.get("estilo_gemini") or "como um narrador de audiolivro: tom natural, calmo e expressivo",
                remover_notas=bool(p.get("remover_notas", True)), biblia=bool(p.get("biblia", True)),
                revisar=bool(p.get("revisar", False)), so_texto=bool(p.get("so_texto", False)),
                drive=False, baixar=False, sem_referencias=bool(p.get("sem_referencias", True))))
            if res:
                t["estado"], t["progresso"], t["arquivo"] = "pronta", 1.0, os.path.basename(res)
            else:
                t["estado"] = "erro"
                t.setdefault("erro", (t.get("registro") or ["Não foi possível gerar."])[-1])
        except Exception as e:
            print(f"Erro: {e}")
            traceback.print_exc(file=sys.__stderr__)
            t["estado"], t["erro"] = "erro", str(e)[:400]
        finally:
            SAIDA.destinos.pop(threading.get_ident(), None)
            t["fim"] = time.time()

threading.Thread(target=_trabalhador, daemon=True).start()


# ---------------- manter acordado durante a geração ----------------
# No plano grátis do Render o servidor dorme após 15 min sem visitas. Enquanto houver áudio
# sendo gerado, o próprio servidor visita o seu endereço público para não ser desligado no meio.
def _manter_acordado():
    import urllib.request
    url = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("URL_PUBLICA") or "").rstrip("/")
    if not url:
        return
    while True:
        time.sleep(240)
        if any(t["estado"] in ("gerando", "na fila") for t in TAREFAS.values()):
            try:
                urllib.request.urlopen(url + "/api/saude", timeout=20).read()
            except Exception:
                pass

threading.Thread(target=_manter_acordado, daemon=True).start()


# ---------------- autenticação simples ----------------
def _autorizado(req: Request):
    if not SENHA:
        return True
    dado = req.headers.get("x-senha") or req.query_params.get("s") or req.cookies.get("senha") or ""
    return hashlib.sha256(dado.encode()).hexdigest() == hashlib.sha256(SENHA.encode()).hexdigest()

def protegido(fn):
    async def interno(req):
        if not _autorizado(req):
            return JSONResponse({"erro": "senha"}, status_code=401)
        try:
            return await fn(req)
        except Exception as e:
            traceback.print_exc(file=sys.__stderr__)
            return JSONResponse({"erro": str(e)[:300]}, status_code=500)
    return interno


def _seguro(nome):
    nome = os.path.basename(nome)
    if not nome or nome.startswith("."):
        raise ValueError("nome inválido")
    return nome


# ---------------- rotas ----------------
async def saude(req):
    return JSONResponse({"ok": True})

async def pagina(req):
    with open(os.path.join(AQUI, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})

async def estatico(req):
    nome = req.path_params["nome"]
    if nome not in {"manifest.json", "icon-192.png", "icon-512.png", "sw.js"}:
        return Response(status_code=404)
    tipo = {"json": "application/manifest+json", "png": "image/png", "js": "text/javascript"}[nome.rsplit(".", 1)[1]]
    return FileResponse(os.path.join(AQUI, nome), media_type=tipo)

@protegido
async def status(req):
    return JSONResponse({"ok": True, "senha": bool(SENHA), "gemini": bool(os.environ.get("GEMINI_API_KEY")),
                         "vozes_gemini": VOZES_GEMINI})

@protegido
async def vozes(req):
    try:
        v = await motor.vozes_edge_pt()
    except Exception:
        v = ["pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural", "pt-BR-AntonioNeural"]
    return JSONResponse({"edge": v, "gemini": VOZES_GEMINI, "gemini_disponivel": bool(os.environ.get("GEMINI_API_KEY"))})

@protegido
async def livros(req):
    meta = _meta_livros()
    lista = [m for m in meta.values() if os.path.exists(m["caminho"])]
    lista.sort(key=lambda m: -m["quando"])
    return JSONResponse(lista)

@protegido
async def livro(req):
    lid = req.path_params["lid"]
    m = _meta_livros().get(lid)
    if not m or not os.path.exists(m["caminho"]):
        return JSONResponse({"erro": "Livro não encontrado. Envie o arquivo de novo."}, status_code=404)
    info = await asyncio.to_thread(_sumario, m["caminho"]) if m["nome"].lower().endswith(".pdf") else {"sumario": [], "paginas": 0, "tem_texto": True}
    return JSONResponse({**m, **info, "tipo": "pdf" if m["nome"].lower().endswith(".pdf") else "txt"})

@protegido
async def enviar_livro(req):
    tipo = req.headers.get("content-type", "")
    if tipo.startswith("multipart/"):
        form = await req.form(max_files=1, max_part_size=300 * 1024 * 1024)
        arq = form.get("arquivo")
        if arq is None or not getattr(arq, "filename", None):
            return JSONResponse({"erro": "Escolha um arquivo PDF ou TXT."}, status_code=400)
        nome = arq.filename
        if not nome.lower().endswith((".pdf", ".txt")):
            return JSONResponse({"erro": "Só aceito arquivos .pdf ou .txt."}, status_code=400)
        tmp = os.path.join(PASTA_LIVROS, f".envio_{uuid.uuid4().hex}")
        with open(tmp, "wb") as f:
            while True:
                bloco = await arq.read(1 << 20)
                if not bloco:
                    break
                f.write(bloco)
        await arq.close()
    else:
        dados = await req.json()
        link = (dados.get("link") or "").strip()
        if not link.startswith("http"):
            return JSONResponse({"erro": "Cole um link que comece com https://"}, status_code=400)
        buf = []
        def baixar():
            ident = threading.get_ident()
            SAIDA.destinos[ident] = buf.append
            try:
                return motor.baixar_link(link)
            finally:
                SAIDA.destinos.pop(ident, None)
        try:
            caminho = await asyncio.to_thread(baixar)
        except Exception as e:
            return JSONResponse({"erro": "Não consegui baixar pelo link. No Google Drive, deixe o arquivo como "
                                         "'Qualquer pessoa com o link' ou envie o PDF direto. (" + str(e)[:150] + ")"},
                                status_code=400)
        if not caminho or not os.path.exists(caminho):
            return JSONResponse({"erro": "O link não trouxe nenhum arquivo."}, status_code=400)
        nome = os.path.basename(caminho)
        tmp = caminho + f".{uuid.uuid4().hex}"
        os.replace(caminho, tmp)
    try:
        info = await asyncio.to_thread(_registrar_livro, tmp, nome)
    except Exception as e:
        return JSONResponse({"erro": f"Não consegui abrir esse arquivo como PDF ({str(e)[:120]})."}, status_code=400)
    return JSONResponse(info)

@protegido
async def apagar_livro(req):
    lid = req.path_params["lid"]
    meta = _meta_livros()
    m = meta.pop(lid, None)
    if m:
        shutil.rmtree(os.path.dirname(m["caminho"]), ignore_errors=True)
        _salvar_meta(meta)
    return JSONResponse({"ok": True})

@protegido
async def criar_tarefa(req):
    p = await req.json()
    m = _meta_livros().get(p.get("livro", ""))
    if not m:
        return JSONResponse({"erro": "Escolha um livro primeiro."}, status_code=400)
    if m["nome"].lower().endswith(".pdf") and not str(p.get("capitulo", "")).strip() and not str(p.get("paginas", "")).strip():
        return JSONResponse({"erro": "Escolha um capítulo ou escreva as páginas."}, status_code=400)
    if p.get("motor") == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        return JSONResponse({"erro": "A voz do Gemini precisa da chave GEMINI_API_KEY nas configurações do Space."},
                            status_code=400)
    p["caminho"] = m["caminho"]
    tid = uuid.uuid4().hex[:10]
    rotulo = p.get("rotulo") or (f"Capítulo {p.get('capitulo')}" if p.get("capitulo") else f"Páginas {p.get('paginas')}")
    TAREFAS[tid] = {"id": tid, "livro": m["nome"], "rotulo": rotulo, "estado": "na fila", "progresso": 0.0,
                    "registro": [], "pedido": p, "criada": time.time(), "so_texto": bool(p.get("so_texto"))}
    FILA.put(tid)
    return JSONResponse({"id": tid})

@protegido
async def tarefas(req):
    lista = sorted(TAREFAS.values(), key=lambda t: -t["criada"])[:30]
    saida = []
    for t in lista:
        d = {k: v for k, v in t.items() if k != "pedido"}
        if t["estado"] == "gerando" and t.get("progresso", 0) > 0.02:
            gasto = time.time() - t["inicio"]
            d["restante"] = int(gasto / t["progresso"] - gasto)
        saida.append(d)
    return JSONResponse(saida)

@protegido
async def cancelar(req):
    t = TAREFAS.get(req.path_params["tid"])
    if t and t["estado"] == "na fila":
        t["estado"] = "cancelada"
    elif t and t["estado"] in ("pronta", "erro", "cancelada"):
        TAREFAS.pop(t["id"], None)
    return JSONResponse({"ok": True})

@protegido
async def audios(req):
    lista = []
    for nome in os.listdir(PASTA_AUDIOS):
        if nome.lower().endswith(".mp3"):
            c = os.path.join(PASTA_AUDIOS, nome)
            txt = nome[:-4] + ".txt"
            lista.append({"nome": nome, "tamanho": os.path.getsize(c), "quando": os.path.getmtime(c),
                          "tem_texto": os.path.exists(os.path.join(PASTA_AUDIOS, txt))})
    lista.sort(key=lambda a: -a["quando"])
    return JSONResponse(lista)

@protegido
async def audio(req):
    nome = _seguro(req.path_params["nome"])
    c = os.path.join(PASTA_AUDIOS, nome)
    if not os.path.exists(c):
        return Response(status_code=404)
    tipo = "audio/mpeg" if nome.lower().endswith(".mp3") else "text/plain; charset=utf-8"
    disp = "attachment" if req.query_params.get("baixar") else "inline"
    from urllib.parse import quote
    return FileResponse(c, media_type=tipo, headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(nome)}"})

@protegido
async def apagar_audio(req):
    nome = _seguro(req.path_params["nome"])
    for ext in (".mp3", ".txt"):
        c = os.path.join(PASTA_AUDIOS, nome.rsplit(".", 1)[0] + ext)
        if os.path.exists(c):
            os.remove(c)
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/", pagina),
    Route("/{nome:str}", estatico),
    Route("/api/saude", saude),
    Route("/api/status", status),
    Route("/api/vozes", vozes),
    Route("/api/livros", livros),
    Route("/api/livros", enviar_livro, methods=["POST"]),
    Route("/api/livros/{lid}", livro),
    Route("/api/livros/{lid}", apagar_livro, methods=["DELETE"]),
    Route("/api/tarefas", tarefas),
    Route("/api/tarefas", criar_tarefa, methods=["POST"]),
    Route("/api/tarefas/{tid}", cancelar, methods=["DELETE"]),
    Route("/api/audios", audios),
    Route("/api/audios/{nome:path}", audio),
    Route("/api/audios/{nome:path}", apagar_audio, methods=["DELETE"]),
])
