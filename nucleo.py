# ============================================================
#  Núcleo: PDF -> texto limpo -> texto pronto para leitura
# ============================================================
import re, os, unicodedata, statistics, hashlib
from collections import Counter

import pdfplumber
from pypdf import PdfReader

# ------------------------------------------------------------
# Utilidades de texto
# ------------------------------------------------------------
LIGADURAS = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}
PONTUACAO_FINAL = tuple('.!?:;…"”»)')
SOBRESCRITOS = "⁰¹²³⁴⁵⁶⁷⁸⁹*†‡"

def _normalizar_chars(s):
    for k, v in LIGADURAS.items():
        s = s.replace(k, v)
    s = s.replace("­", "-").replace("‐", "-").replace("‑", "-")
    s = re.sub("[" + SOBRESCRITOS + "]", "", s)
    s = re.sub(r"[►▶◄◀■□▪▫●○◆◇★☆✓✔➤➔→←•·]+", " ", s)
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"[ \t]+", " ", s).strip()

def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def _chave(s):
    """Chave para comparar títulos/cabeçalhos: minúsculas, sem acento, só letras e números."""
    return re.sub(r"[^a-z0-9]", "", _sem_acento(s.lower()))

def _chave_margem(s):
    s = re.sub(r"\d+", "#", _sem_acento(s.lower()))
    return re.sub(r"\s+", " ", s).strip()

_ROMANO = re.compile(r"^[ivxlcdm]{1,6}$", re.I)

def _eh_numero_pagina(t):
    t = t.strip().strip("-–—·•|[]() ")
    if not t:
        return True
    return bool(re.fullmatch(r"\d{1,4}", t) or _ROMANO.fullmatch(t)
                or re.fullmatch(r"(p[aá]g(ina)?\.?\s*)?\d{1,4}(\s*(de|/)\s*\d{1,4})?", t, re.I))

def _texto_chars(chars):
    """Monta o texto de uma linha a partir das letras, pondo espaço onde há vão."""
    chars = sorted(chars, key=lambda c: c["x0"])
    out, ant = "", None
    for c in chars:
        if ant is not None and c["x0"] - ant["x1"] > max(0.8, c["size"] * 0.18) and not out.endswith(" "):
            out += " "
        out += c["text"]
        ant = c
    return out


def _calha(chars, largura, corpo):
    """Procura o espaço vazio vertical entre duas colunas. Devolve (x_ini, x_fim) ou None."""
    letras = [c for c in chars if c["text"].strip() and c["text"] not in "-\u00ad\u2010\u2011" and abs(c["size"] - corpo) <= max(1.0, corpo * 0.12)]
    if len(letras) < 300:
        return None
    ocup = [0] * (int(largura) + 2)
    for c in letras:
        for x in range(max(0, int(c["x0"])), min(len(ocup), int(c["x1"]) + 1)):
            ocup[x] += 1
    faixa = ocup[int(largura * 0.3):int(largura * 0.7) + 1]
    limiar = max(2, max(faixa) * 0.2)
    melhor, inicio = (0, None), None
    for x in range(int(largura * 0.3), int(largura * 0.7) + 1):
        if ocup[x] <= limiar:
            inicio = x if inicio is None else inicio
            if x - inicio + 1 > melhor[0]:
                melhor = (x - inicio + 1, (inicio, x + 1))
        else:
            inicio = None
    if melhor[0] < 5:
        return None
    a, b = melhor[1]
    lado_e = sum(1 for c in letras if c["x1"] <= a + 1)
    lado_d = sum(1 for c in letras if c["x0"] >= b - 1)
    if lado_e < len(letras) * 0.2 or lado_d < len(letras) * 0.2:
        return None
    return (a, b)


# Pronomes que, depois de hífen no fim da linha, indicam hífen verdadeiro (fazê-lo, dá-se...)
CLITICOS = {"o", "a", "os", "as", "lo", "la", "los", "las", "no", "na", "nos", "nas", "me", "te", "vos",
            "se", "lhe", "lhes", "ia", "ias", "iam", "íamos",
            "á", "ás", "ão", "emos", "eis", "ei"}
# Primeiras partes de palavras compostas que mantêm o hífen (bem-aventurado, recém-nascido...)
PREFIXOS_COMPOSTOS = {"bem", "além", "aquém", "recém", "vice", "pós", "pré", "pró", "ex", "grão", "grã"}

MARCA = "\u2063"   # marca provisória de "hífen de fim de linha" a decidir depois

def _juntar(a, b):
    """Junta duas linhas. Hífen no fim da linha vira uma marca, decidida depois em _resolver_hifens."""
    if not a:
        return b
    if not b:
        return a
    if a.endswith("-") and not a.endswith("--") and b[0].isalpha() and b[0].islower():
        if re.search(r"[\wÀ-ÿ]-$", a):
            return a[:-1] + MARCA + b
        return a + b
    return a + " " + b


_VOGAL_FINAL = "aeiouáéíóúâêôãõ"

def _parece_hifen_verdadeiro(p1, p2):
    """Sem outra pista no texto: decide se 'p1-' + 'p2' é palavra com hífen (fazê-lo) ou só quebra de linha."""
    p1, p2 = p1.lower(), p2.lower()
    if p1 in PREFIXOS_COMPOSTOS:
        return True
    if p2 not in CLITICOS:
        return False
    if p2 in {"no", "na", "nos", "nas"} and not re.search(r"(m|ão|õe|ãe)$", p1):
        return p2 == "nos" and len(p1) >= 4 and p1[-1] in _VOGAL_FINAL
    if p1[-1:] in "áéíóúâêô":
        return True
    return len(p1) >= 3 and (p1[-1] in _VOGAL_FINAL or p1.endswith("m"))


def _resolver_hifens(texto):
    """Usa o próprio livro como dicionário: se 'santuário' aparece inteiro em outro lugar, junta;
    se 'pode-se' aparece com hífen no meio de uma linha, mantém o hífen."""
    if MARCA not in texto:
        return texto
    limpo = re.sub(r"[\wÀ-ÿ]*" + MARCA + r"[\wÀ-ÿ]*", " ", texto).lower()
    cont = Counter(re.findall(r"[\wÀ-ÿ]+(?:-[\wÀ-ÿ]+)*", limpo))
    def decidir(m):
        p1, p2 = m.group(1), m.group(2)
        junto, hifen = (p1 + p2).lower(), (p1 + "-" + p2).lower()
        if cont[hifen] > cont[junto]:
            return p1 + "-" + p2
        if cont[junto] > 0:
            return p1 + p2
        return p1 + ("-" if _parece_hifen_verdadeiro(p1, p2) else "") + p2
    texto = re.sub(r"([\wÀ-ÿ]+)" + MARCA + r"([\wÀ-ÿ]+)", decidir, texto)
    return texto.replace(MARCA, "")


# ------------------------------------------------------------
# Leitura do PDF
# ------------------------------------------------------------
class LivroPDF:
    def __init__(self, caminho):
        self.caminho = caminho
        self.pdf = pdfplumber.open(caminho)
        self.n = len(self.pdf.pages)
        self._corpo = None
        self._cache_linhas = {}

    # -------- tamanho da fonte do texto principal --------
    def tamanho_corpo(self, amostra=None):
        if self._corpo:
            return self._corpo
        if amostra is None:
            passo = max(1, self.n // 25)
            amostra = list(range(0, self.n, passo))[:25]
        cont = Counter()
        for i in amostra:
            for c in self.pdf.pages[i].chars:
                if c["text"].strip():
                    cont[round(c["size"] * 2) / 2] += 1
        self._corpo = cont.most_common(1)[0][0] if cont else 11.0
        return self._corpo

    def tem_texto(self):
        amostra = range(0, self.n, max(1, self.n // 10))
        return sum(len(self.pdf.pages[i].chars) for i in amostra) > 50

    # -------- linhas de uma página, na ordem de leitura (entende 2 colunas) --------
    def linhas(self, i):
        if i in self._cache_linhas:
            return self._cache_linhas[i]
        corpo = self.tamanho_corpo()
        pagina = self.pdf.pages[i]
        w, h = float(pagina.width), float(pagina.height)

        def manter(obj):
            if obj.get("object_type") != "char":
                return True
            t = obj.get("text", "")
            # números/asteriscos pequenos = chamadas de nota de rodapé no meio do texto
            return not (obj["size"] < corpo * 0.78 and (t.isdigit() or t in "*†‡"))

        filtrada = pagina.filter(manter)
        calha = _calha(filtrada.chars, w, corpo)

        def nova(chars, col):
            texto = _normalizar_chars(_texto_chars(chars))
            if not texto:
                return None
            return {"texto": texto, "top": min(c["top"] for c in chars), "bottom": max(c["bottom"] for c in chars),
                    "x0": min(c["x0"] for c in chars), "x1": max(c["x1"] for c in chars), "col": col,
                    "tam": statistics.median(round(c["size"], 1) for c in chars)}

        saida, esq, dir_ = [], [], []
        def descarregar():
            saida.extend(esq); saida.extend(dir_); esq.clear(); dir_.clear()

        for ln in filtrada.extract_text_lines(layout=False, strip=True, return_chars=True):
            chars = [c for c in ln["chars"] if c["text"].strip()]
            if not chars:
                continue
            if calha is None:
                l = nova(chars, 0)
                if l: saida.append(l)
                continue
            a, b = calha
            meio = (a + b) / 2
            chars = [c for c in chars if not (a < (c["x0"] + c["x1"]) / 2 < b and c["size"] < corpo * 0.8)]
            if not chars:
                continue
            na_calha = [c for c in chars if c["text"] not in "-\u00ad\u2010\u2011"
                        and (c["x0"] < meio < c["x1"] or a + 1.5 < (c["x0"] + c["x1"]) / 2 < b - 1.5)]
            lado_e = sum(1 for c in chars if (c["x0"] + c["x1"]) / 2 <= a + 1.5)
            lado_d = sum(1 for c in chars if (c["x0"] + c["x1"]) / 2 >= b - 1.5)
            continua = False
            if na_calha and lado_e >= 12 and lado_d >= 8:
                # duas colunas na mesma altura têm um vão na calha; uma linha da largura toda, não
                ords = sorted(chars, key=lambda c: c["x0"])
                vaos = [d["x0"] - e["x1"] for e, d in zip(ords, ords[1:])
                        if e["x1"] <= b + 8 and d["x0"] >= a - 8]   # vãos que tocam a calha
                continua = bool(vaos) and max(vaos) < max(3.5, corpo * 0.35)
            if na_calha and (continua or not (lado_e >= 12 and lado_d >= 8)):
                # atravessa a calha e não é uma linha de cada coluna: título ou linha da largura toda
                descarregar()
                l = nova(chars, 0)
                if l: saida.append(l)
                continue
            ce =[c for c in chars if (c["x0"] + c["x1"]) / 2 < meio]
            cd = [c for c in chars if (c["x0"] + c["x1"]) / 2 >= meio]
            if ce:
                l = nova(ce, 1)
                if l: esq.append(l)
            if cd:
                l = nova(cd, 2)
                if l: dir_.append(l)
        descarregar()
        self._cache_linhas[i] = (saida, h, w)
        return self._cache_linhas[i]

    # -------- cabeçalhos e rodapés repetidos --------
    def _margens_repetidas(self, paginas):
        cont = Counter()
        corpo = self.tamanho_corpo()
        for i in paginas:
            linhas, h, _ = self.linhas(i)
            for ln in linhas:
                if ln["tam"] >= corpo * 1.2:
                    continue
                if ln["top"] < h * 0.10 or ln["bottom"] > h * 0.90:
                    cont[_chave_margem(ln["texto"])] += 1
        minimo = max(2, int(len(paginas) * 0.2))
        return {k for k, v in cont.items() if v >= minimo and k}

    # -------- sumário --------
    def sumario(self, detectar_se_vazio=True, progresso=None):
        """Lista de capítulos: [{'nivel', 'titulo', 'pagina'(0-based)}]."""
        itens = []
        try:
            leitor = PdfReader(self.caminho)
            def percorrer(lista, nivel):
                for item in lista:
                    if isinstance(item, list):
                        percorrer(item, nivel + 1)
                    else:
                        try:
                            p = leitor.get_destination_page_number(item)
                        except Exception:
                            p = None
                        if p is not None and p >= 0:
                            itens.append({"nivel": nivel, "titulo": str(item.title).strip(), "pagina": p})
            percorrer(leitor.outline, 1)
        except Exception:
            itens = []
        # junta marcadores que são só o número ("1") com o título que vem logo depois, na mesma página
        juntos = []
        for it in itens:
            ant = juntos[-1] if juntos else None
            if (ant and re.fullmatch(r"\s*([0-9]{1,3}|[IVXLC]{1,6})\.?\s*", ant["titulo"])
                    and it["pagina"] == ant["pagina"] and it["nivel"] > ant["nivel"] and not ant.get("_junto")):
                ant["titulo"] = f"{ant['titulo'].strip().rstrip('.')} – {it['titulo']}"
                ant["_junto"] = True
                continue
            juntos.append(it)
        for it in juntos:
            it.pop("_junto", None)
            it["titulo"] = re.sub(r"\s+", " ", it["titulo"])
        itens = juntos
        if itens or not detectar_se_vazio:
            return itens
        if self.n > 300:
            return []          # livro grande sem sumário: não varre tudo, use as páginas
        return self.detectar_titulos(progresso)

    def detectar_titulos(self, progresso=None):
        """Quando o PDF não tem sumário: procura linhas em fonte grande no topo das páginas."""
        corpo = self.tamanho_corpo()
        itens = []
        for i in range(self.n):
            if progresso and i % 25 == 0:
                progresso(i, self.n)
            linhas, h, _ = self.linhas(i)
            grandes = [l for l in linhas if l["tam"] >= corpo * 1.35 and l["top"] < h * 0.6
                       and 2 <= len(l["texto"]) <= 90 and not _eh_numero_pagina(l["texto"])]
            if grandes:
                # junta linhas grandes vizinhas (ex.: "CAPÍTULO 3" + "A FÉ")
                titulo = " ".join(l["texto"] for l in grandes[:3])
                itens.append({"nivel": 1, "titulo": titulo[:120], "pagina": i})
        self._cache_linhas.clear()
        return itens

    # -------- extração principal --------
    def extrair(self, paginas, remover_notas=True, inicio_titulo=None, fim_titulo=None):
        """paginas: lista de índices (0-based) em ordem. Devolve texto em parágrafos."""
        corpo = self.tamanho_corpo([p for p in paginas[:25]])
        vizinhas = sorted(set(range(max(0, paginas[0] - 4), min(self.n, paginas[-1] + 5))))
        repetidas = self._margens_repetidas(vizinhas)

        paragrafos = []   # [(tipo, texto, inicio_de_pagina)]
        for idx, i in enumerate(paginas):
            linhas, h, w = self.linhas(i)
            # 1) tira cabeçalhos, rodapés e números de página
            boas = []
            for k, ln in enumerate(linhas):
                if ln["tam"] >= corpo * 1.2:          # títulos nunca são cabeçalho
                    boas.append(ln)
                    continue
                no_topo = ln["top"] < h * 0.10
                na_margem = no_topo or ln["bottom"] > h * 0.90
                if na_margem and (_chave_margem(ln["texto"]) in repetidas or _eh_numero_pagina(ln["texto"])):
                    continue
                # cabeçalho corrido: primeira linha da página, no topo, em fonte menor
                if no_topo and k == 0 and ln["tam"] <= corpo * 0.9 and len(ln["texto"]) <= 80:
                    continue
                if _eh_numero_pagina(ln["texto"]) and len(ln["texto"]) <= 4:
                    continue
                boas.append(ln)

            # 2) recorte do capítulo no meio da página
            if idx == 0 and inicio_titulo:
                pos = _achar_titulo(boas, inicio_titulo)
                if pos is not None:
                    boas = boas[pos:]
            if idx == len(paginas) - 1 and fim_titulo:
                pos = _achar_titulo(boas, fim_titulo)
                if pos is not None:
                    boas = boas[:pos]

            # 3) notas de rodapé: fonte menor, abaixo do último trecho de texto normal (em cada coluna)
            def eh_corpo(l):
                return abs(l["tam"] - corpo) <= max(0.6, corpo * 0.06)
            if remover_notas:
                limites = {}
                for col in (0, 1, 2):
                    bl = [l["bottom"] for l in boas if eh_corpo(l) and (l["col"] == col or l["col"] == 0)]
                    if bl:
                        limites[col] = max(bl)
                boas = [l for l in boas if not (l["tam"] < corpo * 0.93 and l["col"] in limites
                                                and l["top"] >= limites[l["col"]] - 1)]
            if not boas:
                continue
            bordas = {}
            for col in (0, 1, 2):
                cl = [l for l in boas if l["col"] == col and eh_corpo(l)] or [l for l in boas if l["col"] == col]
                if cl:
                    bordas[col] = (min(l["x0"] for l in cl), max(l["x1"] for l in cl))

            # 4) monta parágrafos
            atual, ant, primeiro = None, None, True
            for ln in boas:
                tipo = "titulo" if ln["tam"] >= corpo * 1.2 else "texto"
                esq, dir_ = bordas.get(ln["col"], (ln["x0"], ln["x1"]))
                novo = False
                if atual is None or tipo != atual[0]:
                    novo = True
                else:
                    fim_frase = ant["texto"].endswith(PONTUACAO_FINAL)
                    recuo = ln["x0"] > esq + corpo * 1.0
                    if ln["col"] != ant["col"] or ln["top"] < ant["top"] - 2:
                        novo = fim_frase and recuo                    # mudou de coluna
                    else:
                        altura = max(1.0, ant["bottom"] - ant["top"])
                        vao = ln["top"] - ant["bottom"]
                        ant_dir = bordas.get(ant["col"], (0, dir_))[1]
                        if vao > max(altura * 0.9, corpo * 0.9):
                            novo = True
                        elif tipo == "texto" and fim_frase and recuo:
                            novo = True                                  # recuo de primeira linha
                        elif tipo == "texto" and fim_frase and ant["x1"] < ant_dir - corpo * 3:
                            novo = True                                  # última linha curta
                if novo:
                    if atual:
                        paragrafos.append(atual)
                    atual = [tipo, ln["texto"], primeiro]
                    primeiro = False
                elif tipo == "titulo":
                    seguido = atual[1].endswith(PONTUACAO_FINAL) or ln["texto"][:1].islower()
                    atual[1] = atual[1] + (" " if seguido else ". ") + ln["texto"]
                else:
                    atual[1] = _juntar(atual[1], ln["texto"])
                ant = ln
            if atual:
                paragrafos.append(atual)

        # 5) junta parágrafos quebrados na virada de página
        final = []
        for tipo, txt, inicio_pag in paragrafos:
            if (final and inicio_pag and tipo == "texto" and final[-1][0] == "texto"
                    and (not final[-1][1].endswith(PONTUACAO_FINAL) or final[-1][1].endswith("-"))
                    and txt[:1].islower()):
                final[-1][1] = _juntar(final[-1][1], txt)
            else:
                final.append([tipo, txt])

        blocos = []
        for tipo, txt in final:
            txt = re.sub(r"\s+", " ", txt).strip()
            if tipo == "titulo" and not txt.endswith(PONTUACAO_FINAL):
                txt += "."
            blocos.append(txt)
        return _resolver_hifens("\n\n".join(blocos))


def _achar_titulo(linhas, titulo):
    sem_num = re.sub(r"^\s*(cap[ií]tulo\s+)?([0-9]+|[IVXLC]+)[.)\s:–—-]*", "", titulo, flags=re.I)
    alvos = [a for a in {_chave(titulo), _chave(sem_num)} if len(a) >= 3]
    for k, ln in enumerate(linhas):
        c = _chave(ln["texto"])
        if len(c) < 3:
            continue
        for alvo in alvos:
            if alvo.startswith(c[:20]) or c.startswith(alvo[:20]):
                # volta para pegar o número do capítulo que está logo acima, sozinho na linha
                while k > 0 and _eh_numero_pagina(linhas[k - 1]["texto"]) and linhas[k - 1]["tam"] > 0:
                    k -= 1
                return k
    return None


# ------------------------------------------------------------
# Seleção: "45-78", "10, 12-15"
# ------------------------------------------------------------
def interpretar_intervalo(texto, maximo):
    paginas = []
    for parte in re.split(r"[,;]\s*", texto.strip()):
        if not parte:
            continue
        m = re.fullmatch(r"(\d+)\s*(?:-|–|a|até)\s*(\d+)", parte.strip())
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            paginas.extend(range(min(a, b), max(a, b) + 1))
        elif parte.strip().isdigit():
            paginas.append(int(parte))
        else:
            raise ValueError(f"Não entendi '{parte}'. Use algo como 45-78 ou 10, 12-15.")
    paginas = [p for p in paginas if 1 <= p <= maximo]
    return sorted(set(paginas))


def faixa_do_capitulo(sumario, numero, total_paginas):
    """Devolve (paginas_0based, titulo_inicio, titulo_fim) para o item 'numero' (1-based) do sumário."""
    item = sumario[numero - 1]
    proximo = None
    for seg in sumario[numero:]:
        if seg["nivel"] <= item["nivel"] and seg["pagina"] >= item["pagina"]:
            proximo = seg
            break
    fim = proximo["pagina"] if proximo else total_paginas - 1
    paginas = list(range(item["pagina"], fim + 1))
    return paginas, item["titulo"], (proximo["titulo"] if proximo else None)


# ------------------------------------------------------------
# Referências bíblicas e abreviações -> forma falada
# ------------------------------------------------------------
LIVROS = [
    # (nome falado, [abreviações], numerado?, gênero do ordinal)
    ("Gênesis", ["Gn", "Gên", "Gen", "Gênesis", "Genesis"], False, None),
    ("Êxodo", ["Êx", "Ex", "Êxodo", "Exodo"], False, None),
    ("Levítico", ["Lv", "Lev", "Levítico", "Levitico"], False, None),
    ("Números", ["Nm", "Núm", "Num", "Números", "Numeros"], False, None),
    ("Deuteronômio", ["Dt", "Deut", "Deuteronômio", "Deuteronomio"], False, None),
    ("Josué", ["Js", "Jos", "Josué", "Josue"], False, None),
    ("Juízes", ["Jz", "Juízes", "Juizes"], False, None),
    ("Rute", ["Rt", "Rute"], False, None),
    ("Samuel", ["Sm", "Sam", "Samuel"], True, "o"),
    ("Reis", ["Rs", "Reis"], True, "o"),
    ("Crônicas", ["Cr", "Crôn", "Crônicas", "Cronicas"], True, "o"),
    ("Esdras", ["Ed", "Esd", "Esdras"], False, None),
    ("Neemias", ["Ne", "Ne", "Neemias"], False, None),
    ("Ester", ["Et", "Est", "Ester"], False, None),
    ("Jó", ["Jó", "Jb"], False, None),
    ("Salmo", ["Sl", "Sal", "Salmo", "Salmos"], False, None),
    ("Provérbios", ["Pv", "Pr", "Prov", "Provérbios", "Proverbios"], False, None),
    ("Eclesiastes", ["Ec", "Ecl", "Eclesiastes"], False, None),
    ("Cântico dos Cânticos", ["Ct", "Cânt", "Cântico dos Cânticos", "Cânticos"], False, None),
    ("Isaías", ["Is", "Isa", "Isaías", "Isaias"], False, None),
    ("Jeremias", ["Jr", "Jer", "Jeremias"], False, None),
    ("Lamentações", ["Lm", "Lam", "Lamentações", "Lamentacoes"], False, None),
    ("Ezequiel", ["Ez", "Ezeq", "Ezequiel"], False, None),
    ("Daniel", ["Dn", "Dan", "Daniel"], False, None),
    ("Oseias", ["Os", "Oseias", "Oséias"], False, None),
    ("Joel", ["Jl", "Joel"], False, None),
    ("Amós", ["Am", "Amós", "Amos"], False, None),
    ("Obadias", ["Ob", "Obd", "Obadias"], False, None),
    ("Jonas", ["Jn", "Jonas"], False, None),
    ("Miqueias", ["Mq", "Miq", "Miqueias", "Miquéias"], False, None),
    ("Naum", ["Na", "Naum"], False, None),
    ("Habacuque", ["Hc", "Hab", "Habacuque"], False, None),
    ("Sofonias", ["Sf", "Sof", "Sofonias"], False, None),
    ("Ageu", ["Ag", "Ageu"], False, None),
    ("Zacarias", ["Zc", "Zac", "Zacarias"], False, None),
    ("Malaquias", ["Ml", "Mal", "Malaquias"], False, None),
    ("Mateus", ["Mt", "Mat", "Mateus"], False, None),
    ("Marcos", ["Mc", "Mar", "Marcos"], False, None),
    ("Lucas", ["Lc", "Luc", "Lucas"], False, None),
    ("João", ["Jo", "João", "Joao"], False, None),
    ("Atos", ["At", "Atos"], False, None),
    ("Romanos", ["Rm", "Rom", "Romanos"], False, None),
    ("Coríntios", ["Co", "Cor", "Coríntios", "Corintios"], True, "a"),
    ("Gálatas", ["Gl", "Gál", "Gal", "Gálatas", "Galatas"], False, None),
    ("Efésios", ["Ef", "Efés", "Efe", "Efésios", "Efesios"], False, None),
    ("Filipenses", ["Fp", "Fl", "Fil", "Filipenses"], False, None),
    ("Colossenses", ["Cl", "Col", "Colossenses"], False, None),
    ("Tessalonicenses", ["Ts", "Tes", "Tessalonicenses"], True, "a"),
    ("Timóteo", ["Tm", "Tim", "Timóteo", "Timoteo"], True, "a"),
    ("Tito", ["Tt", "Tito"], False, None),
    ("Filemom", ["Fm", "Flm", "Filemom", "Filemon"], False, None),
    ("Hebreus", ["Hb", "Heb", "Hebreus"], False, None),
    ("Tiago", ["Tg", "Tiago"], False, None),
    ("Pedro", ["Pe", "Ped", "Pedro"], True, "a"),
    ("João", ["Jo", "João", "Joao"], True, "a"),   # epístolas (com número)
    ("Judas", ["Jd", "Judas"], False, None),
    ("Apocalipse", ["Ap", "Apoc", "Apocalipse"], False, None),
]

# abreviações que também são palavras comuns: só convertem se tiverem versículo (ex.: "Os 3:5")
ARRISCADAS = {"Os", "Na", "Am", "Ag", "Ed", "Et", "Ne", "Ob", "Ex", "Pr", "Fl", "Cl", "Jo", "Jó",
              "Is", "Ct", "At", "João", "Joao", "Mar", "Mal", "Col", "Tito", "Judas", "Rute", "Ester", "Pedro",
              "Lucas", "Marcos", "Mateus", "Daniel", "Joel", "Jonas", "Samuel", "Tiago", "Atos",
              "Isa", "Est", "Sal", "Num", "Gen", "Dan", "Luc", "Jos", "Lev", "Rom", "Hab", "Mat", "Ped"}

_MAPA_SIMPLES, _MAPA_NUM = {}, {}
for nome, abrevs, numerado, genero in LIVROS:
    alvo = _MAPA_NUM if numerado else _MAPA_SIMPLES
    for a in abrevs:
        alvo.setdefault(a, (nome, genero))

def _alternativas(mapa):
    return "|".join(re.escape(a) for a in sorted(mapa, key=len, reverse=True))

_V = r"\d{1,3}[a-d]?"
_SEP_CV = r"\s?[:.,]\s?"
_FAIXA = r"\s?[-–—]\s?"
# capítulo com versículos:  3:16 | 3:16-18 | 3:16,18 | 1:1-2:3 | 3.16 | 3,16.18
_CV = rf"\d{{1,3}}{_SEP_CV}{_V}(?:{_FAIXA}\d{{1,3}}[a-d]?(?:\s?[:.]\s?{_V})?)?(?:\s?[,.]\s?{_V}(?:{_FAIXA}{_V})?)*"
# só capítulo:  23 | 1-3
_C = rf"\d{{1,3}}(?:{_FAIXA}\d{{1,3}})?"
_REFS = rf"(?:{_CV}|{_C})(?:\s?;\s?{_CV})*"

_RX_BIBLIA = re.compile(
    rf"(?<![\wÀ-ÿ])(?:(?P<n>[123l]|III|II|I)\s?[ªºa]?\.?\s?(?P<bn>{_alternativas(_MAPA_NUM)})"
    rf"|(?P<bs>{_alternativas(_MAPA_SIMPLES)}))\.?\s(?P<refs>{_REFS})(?![\d:])"
)

_ORD = {"1": 0, "l": 0, "I": 0, "2": 1, "II": 1, "3": 2, "III": 2}
_ORD_NOMES = {"o": ["Primeiro", "Segundo", "Terceiro"], "a": ["Primeira", "Segunda", "Terceira"]}

def _limpa_v(v):
    return re.sub(r"[a-d]$", "", v.strip())

def _lista_falada(itens):
    if len(itens) == 1:
        return itens[0]
    return ", ".join(itens[:-1]) + " e " + itens[-1]

def _grupo_falado(g, salmo):
    g = g.strip()
    m = re.match(r"(\d{1,3})\s?([:.,])\s?(.*)$", g)
    if not m:  # só capítulo(s)
        r = re.match(r"(\d{1,3})\s?[-–—]\s?(\d{1,3})$", g)
        if r:
            return (f"__SALMOS__{r.group(1)} a {r.group(2)}" if salmo else f"capítulos {r.group(1)} a {r.group(2)}")
        return g if salmo else f"capítulo {g}"
    cap, sep, resto = m.group(1), m.group(2), m.group(3)
    sep_lista = "." if sep == "," else ","
    partes = [p for p in re.split(r"\s?" + re.escape(sep_lista) + r"\s?", resto) if p.strip()]
    falados, plural = [], len(partes) > 1
    for p in partes:
        f = re.match(rf"({_V}){_FAIXA}(\d{{1,3}})(?:\s?[:.]\s?({_V}))?$", p.strip())
        if f and f.group(3):      # cruza capítulos: 1:1-2:3
            return (f"{'' if salmo else 'capítulo '}{cap}, versículo {_limpa_v(f.group(1))}, "
                    f"ao capítulo {f.group(2)}, versículo {_limpa_v(f.group(3))}").strip()
        if f:
            falados.append(f"{_limpa_v(f.group(1))} a {_limpa_v(f.group(2))}")
            plural = True
        else:
            falados.append(_limpa_v(p))
    rot = "versículos" if plural else "versículo"
    prefixo = cap if salmo else f"capítulo {cap}"
    return f"{prefixo}, {rot} {_lista_falada(falados)}"

def _ref_para_fala(m):
    refs = m.group("refs")
    if m.group("bn"):
        abrev = m.group("bn")
        nome, genero = _MAPA_NUM[abrev]
        livro = f"{_ORD_NOMES[genero][_ORD[m.group('n')]]} {nome}"
    else:
        abrev = m.group("bs")
        nome, _ = _MAPA_SIMPLES[abrev]
        livro = nome
        tem_versiculo = re.search(r"\d\s?[:.,]\s?\d", refs)
        if abrev in ARRISCADAS and not tem_versiculo:
            return m.group(0)
        if not tem_versiculo and refs.isdigit() and int(refs) > 150:
            return m.group(0)
    salmo = nome == "Salmo"
    grupos = [g for g in refs.split(";") if g.strip()]
    falados = [_grupo_falado(g, salmo) for g in grupos]
    if salmo:
        r = f"{livro} " + "; Salmo ".join(falados)
        return r.replace("Salmo __SALMOS__", "Salmos ").replace("__SALMOS__", "")
    return f"{livro}, " + "; ".join(falados)

def converter_referencias_biblicas(texto):
    return _RX_BIBLIA.sub(_ref_para_fala, texto)


ABREVIACOES = [
    (r"\bvv\.\s?(\d+)\s?[-–]\s?(\d+)", r"versículos \1 a \2"),
    (r"\bvv\.\s?(\d+)", r"versículos \1"),
    (r"\bv\.\s?(\d+)\s?[-–]\s?(\d+)", r"versículos \1 a \2"),
    (r"\bv\.\s?(\d+)", r"versículo \1"),
    (r"\bCf\.\s?", "Conforme "), (r"\bcf\.\s?", "conforme "),
    (r"\bcaps\.\s?", "capítulos "), (r"\bcap\.\s?", "capítulo "),
    (r"\bpp\.\s?(\d)", r"páginas \1"), (r"\bp\.\s?(\d)", r"página \1"),
    (r"\bop\.\s?cit\.", "obra citada"), (r"\bséc\.\s?", "século "),
    (r"\ba\.\s?C\.", "antes de Cristo"), (r"\bd\.\s?C\.", "depois de Cristo"),
    (r"\bet\s+al\.", "e outros"), (r"\bi\.\s?e\.", "isto é"), (r"\be\.\s?g\.", "por exemplo"),
]

_ITEM_SUMARIO = re.compile(r"(?:(?<=\s)|^)(?:[IVXL]{1,5}|[A-H]|\d{1,2})\s?\.\s?(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9])")

def remover_sumario_interno(texto):
    """Tira o índice de seções que alguns livros põem no começo de cada capítulo
    ("I. Revelação A. Definição B. Terminologia ...")."""
    pars, saida, pos = texto.split("\n\n"), [], 0
    limite = len(texto) * 0.25
    for p in pars:
        if pos < limite:
            idx = [m.start() for m in _ITEM_SUMARIO.finditer(p)]
            if len(idx) >= 5:
                segs = list(zip(idx, idx[1:] + [len(p)]))
                curtos = [1 for a, b in segs if b - a < 90]
                if len(curtos) >= 5 and idx[0] < 120:
                    longo = next((a for a, b in segs if b - a >= 120), None)
                    pos += len(p) + 2
                    if longo is None:
                        continue
                    p = p[longo:]
        saida.append(p)
        pos += len(p) + 2
    return "\n\n".join(saida)


def preparar_para_leitura(texto, biblia=True):
    if biblia:
        texto = converter_referencias_biblicas(texto)
    for padrao, troca in ABREVIACOES:
        texto = re.sub(padrao, troca, texto)
    # números de nota que sobraram grudados: "palavra.12 " / "palavra12 "
    texto = re.sub(r"(?<=[a-zà-ÿ]{3}[.,;:!?»”\"])\d{1,3}(?=\s|$)", "", texto)
    texto = re.sub(r"(?<=[a-zà-ÿ]{3})\d{1,3}(?=[\s.,;:!?]|$)", "", texto)
    # URLs atrapalham a leitura
    texto = re.sub(r"https?://\S+|www\.\S+", "", texto)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


# ------------------------------------------------------------
# Divisão em pedaços (para a voz e para o Gemini)
# ------------------------------------------------------------
def dividir(texto, limite):
    pedacos, atual = [], ""
    for par in texto.split("\n\n"):
        if len(par) > limite:  # parágrafo enorme: divide por frases
            frases = re.split(r"(?<=[.!?…])\s+", par)
        else:
            frases = [par]
        for fr in frases:
            sep = "\n\n" if fr is frases[0] else " "
            if atual and len(atual) + len(fr) + 2 > limite:
                pedacos.append(atual)
                atual = fr
            else:
                atual = (atual + sep + fr) if atual else fr
    if atual:
        pedacos.append(atual)
    return pedacos

def nome_seguro(s, maximo=60):
    s = _sem_acento(s)
    s = re.sub(r"[^\w\s-]", "", s).strip()
    return re.sub(r"[\s]+", "_", s)[:maximo] or "audio"

def assinatura(texto, extra=""):
    return hashlib.md5((texto + "|" + extra).encode("utf-8")).hexdigest()[:12]
