# ============================================================
#  Vozes (Edge e Gemini) e geração do MP3
# ============================================================
import os, re, time, base64, shutil, asyncio, subprocess, getpass
try:                      # fora do Colab (no servidor) o núcleo vem de outro arquivo
    from nucleo import *  # noqa: F401,F403
except ImportError:
    pass

PASTA_DOWNLOAD = "/content" if os.path.isdir("/content") else os.path.abspath("livros")
PASTA_AUDIOS = "/content/audios" if os.path.isdir("/content") else os.path.abspath("audios")
PASTA_DRIVE = "/content/drive/MyDrive/Audiolivros"
LIVRO, SUMARIO, TEXTO_TXT, NOME_BASE = None, [], None, "livro"

MODELOS_TTS_GEMINI = ["gemini-3.1-flash-tts-preview", "gemini-3.8-flash-tts", "gemini-2.5-flash-preview-tts"]
MODELOS_TEXTO_GEMINI = ["gemini-3.1-flash-lite", "gemini-2.5-flash"]
_GEMINI = None


class CotaEsgotada(Exception):
    pass


# ---------------- carregar o arquivo ----------------
TITULOS_REFERENCIAS = re.compile(
    r"^\s*(?:[IVXLC]+\.|\d+\.)?\s*(refer[êe]ncias(?: bibliogr[áa]ficas)?|bibliografia|literatura|obras citadas|"
    r"obras consultadas|notas|leituras? (?:recomendadas|adicionais))\s*\.?\s*$", re.I)


def baixar_link(url):
    """Baixa um PDF a partir de um link do Google Drive (ou outro link direto)."""
    m = re.search(r"/d/([\w-]{20,})|[?&]id=([\w-]{20,})", url)
    if m and "google" in url:
        fid = m.group(1) or m.group(2)
        try:
            from google.colab import auth
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload
            print("🔑 Autorize o acesso ao seu Google Drive na janela que abrir...")
            auth.authenticate_user()
            srv = build("drive", "v3")
            meta = srv.files().get(fileId=fid, fields="name,size", supportsAllDrives=True).execute()
            destino = os.path.join(PASTA_DOWNLOAD, meta["name"])
            with open(destino, "wb") as f:
                baixa = MediaIoBaseDownload(f, srv.files().get_media(fileId=fid, supportsAllDrives=True),
                                            chunksize=8 * 1024 * 1024)
                pronto = False
                while not pronto:
                    estado, pronto = baixa.next_chunk()
                    if estado:
                        print(f"   baixando... {estado.progress() * 100:.0f}%", end="\r")
            print(f"\n⬇️ Baixado: {meta['name']}")
            return destino
        except Exception as e:
            print(f"   (não consegui pelo acesso direto: {str(e)[:120]}; tentando pelo link compartilhado)")
            import gdown
            os.makedirs(PASTA_DOWNLOAD, exist_ok=True)
            caminho = gdown.download(id=fid, output=PASTA_DOWNLOAD + "/", quiet=False)
            if not caminho:
                raise RuntimeError("Não consegui baixar. No Drive, compartilhe o arquivo como "
                                   "'Qualquer pessoa com o link' ou envie o PDF pelo botão da etapa 2.")
            return caminho
    import urllib.request
    os.makedirs(PASTA_DOWNLOAD, exist_ok=True)
    destino = os.path.join(PASTA_DOWNLOAD, os.path.basename(url.split("?")[0]) or "arquivo.pdf")
    urllib.request.urlretrieve(url, destino)
    return destino


def carregar(caminho, mostrar_subtitulos=False):
    global LIVRO, SUMARIO, TEXTO_TXT, NOME_BASE
    caminho = caminho.strip().strip('"').strip("'")
    if caminho.lower().startswith("http"):
        caminho = baixar_link(caminho)
    if not os.path.exists(caminho):
        print(f"⚠️ Não achei o arquivo: {caminho}")
        print("   Cole o link do Drive (Compartilhar › Copiar link) ou o caminho, como /content/drive/MyDrive/Livros/livro.pdf")
        return
    NOME_BASE = nome_seguro(os.path.splitext(os.path.basename(caminho))[0], 50)
    LIVRO, SUMARIO, TEXTO_TXT = None, [], None
    if caminho.lower().endswith(".txt"):
        with open(caminho, "r", encoding="utf-8", errors="replace") as f:
            TEXTO_TXT = f.read()
        print(f"📄 Texto carregado: {len(TEXTO_TXT):,} caracteres. Vá para a etapa 3 (deixe capítulo e páginas em branco).".replace(",", "."))
        return
    LIVRO = LivroPDF(caminho)
    print(f"📕 {os.path.basename(caminho)} — {LIVRO.n} páginas")
    print("   (nada é formatado agora: só as páginas que você escolher na etapa 3 serão lidas e limpas)")
    if not LIVRO.tem_texto():
        print("⚠️ Este PDF parece ser só imagem (digitalizado). Ele precisa de OCR antes; o texto não pode ser lido diretamente.")
        return
    SUMARIO = LIVRO.sumario(progresso=lambda i, n: print(f"   procurando capítulos... página {i}/{n}", end="\r"))
    if not SUMARIO:
        print("\nEste PDF não tem sumário embutido. Use o campo 'paginas' na etapa 3 (ex.: 45-78).")
        return
    nivel_max = min(it["nivel"] for it in SUMARIO) + 1
    print("\n📑 SUMÁRIO — use o número da esquerda no campo 'capitulo':\n")
    ocultos = 0
    for k, it in enumerate(SUMARIO, 1):
        if not mostrar_subtitulos and it["nivel"] > nivel_max:
            ocultos += 1
            continue
        recuo = "   " * (it["nivel"] - 1)
        print(f"{k:>4}. {recuo}{it['titulo'][:80]}  (pág. {it['pagina'] + 1})")
    if ocultos:
        print(f"\n({ocultos} subtítulos escondidos; marque 'mostrar_subtitulos' para ver todos)")
    print("Dica: para juntar capítulos, escreva por exemplo 3-5.")


def _corte_referencias(num, ate_pagina):
    """Procura, dentro do capítulo, um subtítulo do sumário como 'Referências' ou 'Literatura'."""
    item = SUMARIO[num - 1]
    for seg in SUMARIO[num:]:
        if seg["nivel"] <= item["nivel"] or seg["pagina"] > ate_pagina:
            break
        if TITULOS_REFERENCIAS.match(seg["titulo"]):
            return seg
    return None


def _cortar_texto_nas_referencias(texto):
    """Corta a bibliografia do fim do capítulo, mesmo quando o título dela veio grudado no texto."""
    pars = texto.split("\n\n")
    for k in range(len(pars) - 1, int(len(pars) * 0.5), -1):
        if TITULOS_REFERENCIAS.match(pars[k].strip().rstrip(".")):
            return "\n\n".join(pars[:k])
    marcas = [mm.start() for mm in re.finditer(
        r"(?:(?<=\s)|^)(?:[IVXLC]{1,6}\.|\d{1,2}\.)\s*(?:Refer[êe]ncias(?: Bibliogr[áa]ficas)?|Bibliografia|Literatura|Obras Citadas)"
        r"(?=\s+[A-ZÁÉÍÓÚ\"“-])", texto)]
    marcas = [p for p in marcas if p > len(texto) * 0.5]
    if marcas:
        return texto[:marcas[-1]].rstrip()
    return texto


# ---------------- Gemini ----------------
def _cliente_gemini():
    global _GEMINI
    if _GEMINI:
        return _GEMINI
    from google import genai
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        try:
            from google.colab import userdata
            chave = userdata.get("GEMINI_API_KEY")
        except Exception:
            chave = None
    if not chave:
        chave = getpass.getpass("Cole sua chave do Gemini (crie em aistudio.google.com/apikey) e tecle Enter: ").strip()
        os.environ["GEMINI_API_KEY"] = chave
    _GEMINI = genai.Client(api_key=chave)
    return _GEMINI


def _erro_de_modelo(msg):
    m = msg.lower()
    return "404" in m or "not found" in m or "is not supported" in m or "unsupported" in m


def _erro_de_cota(msg):
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


def _cota_diaria(msg):
    return "PerDay" in msg or "per day" in msg.lower() or "daily" in msg.lower()


INSTRUCAO_REVISAO = (
    "Você recebe um trecho extraído de um PDF de livro, que será lido em voz alta. "
    "Corrija SOMENTE problemas de extração: palavras partidas por hífen ou espaço, números de página, "
    "cabeçalhos ou rodapés que sobraram no meio do texto, quebras de parágrafo erradas e números de nota soltos. "
    "NÃO resuma, NÃO reescreva frases, NÃO troque palavras, NÃO traduza e NÃO acrescente comentários. "
    "Mantenha as referências bíblicas exatamente como estão. Devolva apenas o texto corrigido, "
    "com os parágrafos separados por uma linha em branco.\n\nTEXTO:\n"
)


def revisar_com_gemini(texto):
    from google.genai import types  # noqa: F401
    cli = _cliente_gemini()
    partes = dividir(texto, 12000)
    saida = []
    for k, p in enumerate(partes, 1):
        print(f"   🧹 revisando com Gemini {k}/{len(partes)}...")
        novo = None
        for modelo in MODELOS_TEXTO_GEMINI:
            for tentativa in range(5):
                try:
                    r = cli.models.generate_content(model=modelo, contents=INSTRUCAO_REVISAO + p)
                    novo = (r.text or "").strip()
                    break
                except Exception as e:
                    msg = str(e)
                    if _erro_de_modelo(msg):
                        break
                    if _erro_de_cota(msg) and not _cota_diaria(msg):
                        time.sleep(30)
                        continue
                    print(f"   ⚠️ revisão falhou ({msg[:120]}); mantendo o texto original deste trecho.")
                    break
            if novo:
                break
        if novo and 0.85 <= len(novo) / max(1, len(p)) <= 1.15:
            saida.append(novo)
        else:
            saida.append(p)
    return "\n\n".join(saida)


class RespostaSemAudio(Exception):
    pass


def _extrair_audio(r):
    """Procura o áudio em qualquer parte da resposta; se não houver, diz o motivo."""
    motivo = ""
    for cand in (getattr(r, "candidates", None) or []):
        motivo = str(getattr(cand, "finish_reason", "") or motivo)
        conteudo = getattr(cand, "content", None)
        for parte in (getattr(conteudo, "parts", None) or []):
            inline = getattr(parte, "inline_data", None)
            dados = getattr(inline, "data", None) if inline else None
            if dados:
                return base64.b64decode(dados) if isinstance(dados, str) else dados
    fb = getattr(r, "prompt_feedback", None)
    if fb and getattr(fb, "block_reason", None):
        motivo = f"bloqueado: {fb.block_reason}"
    raise RespostaSemAudio(motivo or "resposta vazia")


def _gemini_chamada(texto, voz, estilo, modelo, variante):
    from google.genai import types
    cli = _cliente_gemini()
    if variante == 0:
        # Formato recomendado para o Gemini TTS: as orientações ficam em seções marcadas e só o que
        # vem depois de "#### TRANSCRIPT" é falado.
        prompt = (
            "Synthesize speech for the performance defined below. The profile, scene and performance "
            "sections are direction only. Do NOT speak them. Speak ONLY the text under #### TRANSCRIPT, "
            "word for word, in Brazilian Portuguese.\n\n"
            "# AUDIO PROFILE: Narrador\n"
            "Narrador brasileiro de audiolivros de teologia e estudo bíblico.\n\n"
            "## SCENE:\n"
            "Um estúdio de gravação de audiolivros. O narrador lê para um ouvinte atento.\n\n"
            "### PERFORMANCE\n"
            f"Style: {estilo}.\n"
            "Pace: natural, com pausas curtas entre frases e parágrafos.\n"
            "Accent: português do Brasil.\n\n"
            f"#### TRANSCRIPT\n{texto}"
        )
    else:
        prompt = texto            # sem instrução nenhuma: só o texto
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voz))))
    r = cli.models.generate_content(model=modelo, contents=prompt, config=config)
    return _extrair_audio(r)


def _gemini_tentar(texto, voz, estilo):
    """Tenta os modelos e duas formas de pedido. Devolve PCM ou levanta RespostaSemAudio."""
    ultimo = ""
    for modelo in list(MODELOS_TTS_GEMINI):
        for variante in (0, 1):
            for tentativa in range(4):
                try:
                    pcm = _gemini_chamada(texto, voz, estilo, modelo, variante)
                    MODELOS_TTS_GEMINI.remove(modelo)
                    MODELOS_TTS_GEMINI.insert(0, modelo)
                    return pcm
                except RespostaSemAudio as e:
                    ultimo = f"{modelo}: {e}"
                    break                       # troca a forma do pedido
                except Exception as e:
                    msg = str(e)
                    ultimo = f"{modelo}: {msg[:200]}"
                    if _erro_de_modelo(msg):
                        break
                    if _erro_de_cota(msg):
                        if _cota_diaria(msg):
                            raise CotaEsgotada(msg)
                        print("   ⏳ limite por minuto do Gemini; aguardando 60 s...")
                        time.sleep(60)
                        continue
                    if "API key" in msg or "API_KEY" in msg or "401" in msg or "403" in msg:
                        raise RuntimeError("A chave do Gemini foi recusada. Confira se copiou a chave inteira "
                                           "(aistudio.google.com/apikey) e rode a etapa 1 de novo.")
                    time.sleep(8)
            else:
                continue
            if _erro_de_modelo(ultimo):
                break
    raise RespostaSemAudio(ultimo)


async def _edge_como_pcm(texto, destino_pcm):
    """Último recurso para um trecho que o Gemini recusa: gera com a voz do Edge no mesmo formato."""
    tmp = destino_pcm + ".edge.mp3"
    await _edge_pedaco(texto, "pt-BR-AntonioNeural", "+0%", tmp)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-f", "s16le", "-ar", "24000", "-ac", "1",
                    destino_pcm + ".tmp"], check=True)
    os.remove(tmp)
    os.replace(destino_pcm + ".tmp", destino_pcm)


def _gemini_recursivo(texto, voz, estilo, nivel=0):
    try:
        return _gemini_tentar(texto, voz, estilo)
    except RespostaSemAudio as e:
        if len(texto) > 400 and nivel < 1:
            # divide o trecho ao meio, numa frase, e tenta cada metade
            meio = len(texto) // 2
            corte = max(texto.rfind(". ", 0, meio), texto.rfind("\n", 0, meio))
            corte = corte + 1 if corte > len(texto) * 0.2 else meio
            print(f"   ↪️ o Gemini não devolveu áudio ({str(e)[:80]}); dividindo o trecho e tentando de novo...")
            return (_gemini_recursivo(texto[:corte].strip(), voz, estilo, nivel + 1) + b"\x00\x00" * 4800 +
                    _gemini_recursivo(texto[corte:].strip(), voz, estilo, nivel + 1))
        raise


def _gemini_pedaco(texto, voz, estilo, destino):
    pcm = _gemini_recursivo(texto, voz, estilo)     # levanta RespostaSemAudio se o Gemini recusar mesmo
    with open(destino + ".tmp", "wb") as f:
        f.write(pcm)
    os.replace(destino + ".tmp", destino)


# ---------------- Edge ----------------
async def _edge_pedaco(texto, voz, velocidade, destino):
    import edge_tts
    for tentativa in range(5):
        try:
            com = edge_tts.Communicate(texto, voz, rate=velocidade)
            await com.save(destino + ".tmp")
            os.replace(destino + ".tmp", destino)
            return
        except Exception as e:
            espera = 5 * (tentativa + 1)
            print(f"   ⚠️ falha na voz ({e.__class__.__name__}); tentando de novo em {espera} s...")
            await asyncio.sleep(espera)
    raise RuntimeError("O serviço de voz não respondeu. Rode a etapa 3 de novo: o que já ficou pronto é aproveitado.")


async def vozes_edge_pt():
    import edge_tts
    vozes = await edge_tts.list_voices()
    return sorted(v["ShortName"] for v in vozes if v["ShortName"].startswith("pt-BR"))


# ---------------- juntar as partes ----------------
def _juntar_mp3(partes, saida):
    lista = saida + ".lista.txt"
    with open(lista, "w", encoding="utf-8") as f:
        for p in partes:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lista,
                        "-c", "copy", saida], capture_output=True)
    os.remove(lista)
    if r.returncode != 0:
        with open(saida, "wb") as out:
            for p in partes:
                with open(p, "rb") as f:
                    out.write(f.read())


def _pcm_para_mp3(partes, saida):
    bruto = saida + ".pcm"
    with open(bruto, "wb") as out:
        for p in partes:
            with open(p, "rb") as f:
                out.write(f.read())
            out.write(b"\x00\x00" * 12000)   # meio segundo de pausa entre partes
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", bruto,
                    "-codec:a", "libmp3lame", "-b:a", "64k", saida], check=True)
    os.remove(bruto)


def _duracao(caminho):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=nw=1:nk=1", caminho], capture_output=True, text=True)
        s = float(r.stdout.strip())
        return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}min" if s >= 3600 else f"{int(s // 60)}min{int(s % 60):02d}s"
    except Exception:
        return "?"


# ---------------- tudo junto ----------------
async def gerar(capitulo="", paginas="", motor="Edge", voz_edge="pt-BR-FranciscaNeural", velocidade_edge=0,
                voz_gemini="Kore", estilo_gemini="como um narrador de audiolivro", remover_notas=True,
                biblia=True, revisar=False, so_texto=False, drive=True, baixar=True, sem_referencias=True):
    if LIVRO is None and TEXTO_TXT is None:
        print("⚠️ Primeiro rode a etapa 2 e envie o PDF.")
        return None

    # 1) escolher o trecho
    if TEXTO_TXT is not None:
        texto, rotulo = TEXTO_TXT, "texto"
    elif str(capitulo).strip():
        if not SUMARIO:
            print("⚠️ Este PDF não tem sumário. Use o campo 'paginas'.")
            return None
        nums = interpretar_intervalo(str(capitulo), len(SUMARIO))
        if not nums:
            print(f"⚠️ Escolha um número de 1 a {len(SUMARIO)}.")
            return None
        p1, ini, _ = faixa_do_capitulo(SUMARIO, nums[0], LIVRO.n)
        p2, _, fim = faixa_do_capitulo(SUMARIO, nums[-1], LIVRO.n)
        if sem_referencias:
            ref = _corte_referencias(nums[-1], p2[-1])
            if ref:
                p2, fim = list(range(p2[0], ref["pagina"] + 1)), ref["titulo"]
        pags = list(range(p1[0], p2[-1] + 1))
        rotulo = SUMARIO[nums[0] - 1]["titulo"] + (f" a {SUMARIO[nums[-1] - 1]['titulo']}" if len(nums) > 1 else "")
        print(f"📖 {rotulo} — páginas {pags[0] + 1} a {pags[-1] + 1}")
        texto = LIVRO.extrair(pags, remover_notas, ini, fim)
        texto = remover_sumario_interno(texto)
        if sem_referencias:
            texto = _cortar_texto_nas_referencias(texto)
    elif str(paginas).strip():
        pags = [p - 1 for p in interpretar_intervalo(str(paginas), LIVRO.n)]
        if not pags:
            print(f"⚠️ Páginas fora do livro (ele tem {LIVRO.n}).")
            return None
        rotulo = f"paginas {pags[0] + 1}-{pags[-1] + 1}"
        print(f"📖 Páginas {pags[0] + 1} a {pags[-1] + 1}")
        texto = LIVRO.extrair(pags, remover_notas)
    else:
        print("⚠️ Preencha 'capitulo' (número do sumário) ou 'paginas' (ex.: 45-78).")
        return None

    if not texto.strip():
        print("⚠️ Não saiu texto desse trecho. Confira os números.")
        return None

    # 2) limpeza extra opcional e preparação para leitura
    if revisar:
        texto = revisar_com_gemini(texto)
    fala = preparar_para_leitura(texto, biblia)

    # 3) pastas
    if drive:
        try:
            from google.colab import drive as gdrive
            if not os.path.isdir("/content/drive/MyDrive"):
                gdrive.mount("/content/drive")
            os.makedirs(PASTA_DRIVE, exist_ok=True)
        except Exception as e:
            print(f"⚠️ Não consegui abrir o Google Drive ({e}); o áudio fica só aqui no Colab.")
            drive = False
    os.makedirs(PASTA_AUDIOS, exist_ok=True)
    nome = f"{NOME_BASE} - {nome_seguro(rotulo, 70)}"
    caminho_txt = os.path.join(PASTA_AUDIOS, nome + ".txt")
    with open(caminho_txt, "w", encoding="utf-8") as f:
        f.write(fala)

    minutos = len(fala) / 900
    print(f"📝 {len(fala):,} caracteres (~{minutos:.0f} min de áudio em velocidade normal)".replace(",", "."))
    if so_texto:
        print("\n" + "─" * 70 + "\n" + fala[:4000] + ("\n[...]" if len(fala) > 4000 else ""))
        print("─" * 70 + f"\nTexto completo salvo em: {caminho_txt}")
        return caminho_txt

    # 4) voz
    usa_gemini = motor.lower().startswith("gemini")
    if usa_gemini:
        limite, ext, extra = 1500, ".pcm", f"gemini-v2|{voz_gemini}|{estilo_gemini}"
    else:
        disponiveis = await vozes_edge_pt()
        if voz_edge not in disponiveis:
            print(f"⚠️ A voz {voz_edge} não está disponível agora. Usando pt-BR-FranciscaNeural.")
            print("   Vozes disponíveis:", ", ".join(disponiveis))
            voz_edge = "pt-BR-FranciscaNeural"
        vel = f"{int(velocidade_edge):+d}%"
        limite, ext, extra = 4000, ".mp3", f"edge|{voz_edge}|{vel}"

    pedacos = dividir(fala, limite)
    base_trab = os.path.join(PASTA_DRIVE if drive else PASTA_AUDIOS, ".trabalho")
    trab = os.path.join(base_trab, nome_seguro(nome, 60) + "_" + assinatura(fala, extra))
    os.makedirs(trab, exist_ok=True)
    partes = [os.path.join(trab, f"parte_{k:04d}{ext}") for k in range(len(pedacos))]
    print(f"🔊 Gerando {len(pedacos)} parte(s) com {'Gemini · ' + voz_gemini if usa_gemini else voz_edge}...")

    inicio = time.time()
    recusas_seguidas = 0
    for k, (pedaco, destino) in enumerate(zip(pedacos, partes), 1):
        if os.path.exists(destino) and os.path.getsize(destino) > 0:
            continue
        try:
            if usa_gemini:
                try:
                    _gemini_pedaco(pedaco, voz_gemini, estilo_gemini, destino)
                except RespostaSemAudio as e:
                    recusas_seguidas += 1
                    if recusas_seguidas >= 2:
                        print(f"\n⛔ O Gemini não está devolvendo áudio ({str(e)[:150]}).")
                        print("   Isso costuma ser instabilidade do modelo de voz, que ainda está em teste no Google.")
                        print("   O que já ficou pronto está salvo. Tente de novo mais tarde (ele continua daqui)")
                        print("   ou troque o motor para Edge, que não tem esse problema.")
                        return None
                    print(f"\n   ⚠️ O Gemini não leu um trecho ({str(e)[:120]}). Ele será lido com a voz do Edge "
                          f"para o áudio não ficar com buraco:\n      “{pedaco[:90]}...”")
                    await _edge_como_pcm(pedaco, destino)
                    continue
                recusas_seguidas = 0
            else:
                await _edge_pedaco(pedaco, voz_edge, vel, destino)
        except CotaEsgotada:
            feitas = sum(os.path.exists(p) for p in partes)
            print(f"\n⛔ A cota diária grátis do Gemini acabou ({feitas} de {len(partes)} partes prontas).")
            print("   O progresso está salvo. Amanhã rode a etapa 3 com as mesmas opções e ele continua daqui,")
            print("   ou troque o motor para Edge para terminar agora (começa do zero com a outra voz).")
            return None
        decorrido = time.time() - inicio
        print(f"   ✅ parte {k}/{len(pedacos)}  ({decorrido / 60:.1f} min)", end="\r")

    # 5) juntar e entregar
    saida = os.path.join(PASTA_AUDIOS, nome + ".mp3")
    if usa_gemini:
        _pcm_para_mp3(partes, saida)
    else:
        _juntar_mp3(partes, saida)
    print(f"\n🎉 Pronto: {os.path.basename(saida)}  ({_duracao(saida)})")

    if drive:
        shutil.copy(saida, os.path.join(PASTA_DRIVE, os.path.basename(saida)))
        shutil.copy(caminho_txt, os.path.join(PASTA_DRIVE, os.path.basename(caminho_txt)))
        print(f"☁️ Salvo no Google Drive em: Meu Drive/Audiolivros/")
        shutil.rmtree(trab, ignore_errors=True)
    try:
        from IPython.display import Audio, display
        if os.path.getsize(saida) < 15_000_000:
            display(Audio(saida))
        if baixar and not drive:
            from google.colab import files
            files.download(saida)
    except Exception:
        pass
    return saida
