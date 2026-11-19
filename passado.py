"""Cortes de partes da live que JÁ PASSARAM (gravação/VOD da Kick).

Procurar um momento pelo que o streamer falou (usa a legenda automática da Kick):
    py passado.py buscar gabepeixe carta colecionavel

Cortar um horário (de hoje) — o Claude acha o melhor trecho dentro dele, com gancho e legenda:
    py passado.py cortar gabepeixe 12:30 12:34 "acha uma carta colecionável no cofre"

Pasta de entrada: coloque qualquer vídeo em J:/cortes-live/entrada/<streamer>/ e ele vira corte
(o iniciar.bat já deixa isso ligado; ou rode:  py passado.py pasta)

O corte vai pra mesma pasta/página de revisão dos cortes ao vivo.
"""
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta

import cortes

UA = {"User-Agent": "Mozilla/5.0"}
PAST = os.path.join(cortes.DATA, "passado")
cortes.WORK = PAST  # este processo trabalha numa pasta separada da gravação ao vivo


def http(url, binary=False, tries=3):
    for k in range(tries):
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(1 + k)


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s)


def kick_vods(ch):
    url = cortes.CFG["canais"][ch]["url"]
    if "kick.com" not in url:
        sys.exit(f"{ch} é da Twitch — por enquanto o corte do passado só funciona com canais da Kick.")
    slug = url.rstrip("/").split("/")[-1]
    vods = json.loads(http(f"https://kick.com/api/v2/channels/{slug}/videos"))
    out = []
    for v in vods:
        src = v.get("source") or ""
        if not src.endswith("master.m3u8"):
            continue
        out.append({"id": v["id"], "titulo": v.get("session_title", ""), "base": src[: -len("master.m3u8")],
                    "ao_vivo": v.get("is_live")})
    return out


def playlist(base, path):
    """[(início epoch, duração, url)] a partir de uma playlist com PROGRAM-DATE-TIME."""
    segs, t, dur = [], None, 0.0
    for line in http(base + path).splitlines():
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            t = datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00")).timestamp()
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and t is not None:
            segs.append((t, dur, base + path.rsplit("/", 1)[0] + "/" + line))
    return segs


def vod_for(ch, quando):
    """Acha a gravação que contém o horário (epoch)."""
    for v in kick_vods(ch):
        segs = playlist(v["base"], "1080p60/playlist.m3u8")
        if segs and segs[0][0] <= quando <= segs[-1][0] + segs[-1][1]:
            return v, segs
    sys.exit("Não achei gravação da Kick cobrindo esse horário (a live pode ter sido de outro dia ou o VOD foi apagado).")


def hora(s, ref=None):
    """'12:30' ou '12:30:15' de hoje (ou de ontem, se ainda não chegou) -> epoch."""
    ref = ref or datetime.now()
    p = [int(x) for x in s.split(":")]
    d = ref.replace(hour=p[0], minute=p[1], second=p[2] if len(p) > 2 else 0, microsecond=0)
    if d > ref + timedelta(minutes=1):
        d -= timedelta(days=1)
    return d.timestamp()


# ------------------------------------------------------------------ buscar
def legendas(ch, v):
    os.makedirs(os.path.join(PAST, ch), exist_ok=True)
    cache_f = os.path.join(PAST, ch, f"legendas_{v['id']}.json")
    cache = json.load(open(cache_f, encoding="utf-8")) if os.path.exists(cache_f) else {}
    segs = playlist(v["base"], "subtitles/pt/playlist.m3u8")
    falta = [s for s in segs if s[2] not in cache]
    if falta:
        print(f"baixando {len(falta)} pedaços de legenda (só na 1ª busca demora; depois fica salvo)...")

        def get(s):
            try:
                txt = http(s[2])
            except Exception:
                return s, None
            linhas = [x for x in txt.splitlines() if x and "-->" not in x and not x.startswith(("WEBVTT", "X-TIMESTAMP"))]
            return s, " ".join(linhas)

        with cf.ThreadPoolExecutor(32) as ex:
            for s, txt in ex.map(get, falta):
                if txt is not None:
                    cache[s[2]] = [s[0], txt]
        json.dump(cache, open(cache_f, "w", encoding="utf-8"), ensure_ascii=False)
    return sorted(cache.values())


def buscar(ch, termos):
    v = kick_vods(ch)[0]
    print(f"live: {v['titulo']}")
    alvo = [norm(t) for t in termos]
    achados = []
    for t, txt in legendas(ch, v):
        if all(a in norm(txt) for a in alvo):
            if achados and t - achados[-1][0] < 60:
                continue
            achados.append((t, txt))
    if not achados:
        print("nada encontrado. Tente outra palavra (a legenda automática erra algumas).")
    for t, txt in achados:
        i = norm(txt).find(alvo[0])
        print(f"{datetime.fromtimestamp(t):%H:%M:%S}  ...{txt[max(0, i - 90): i + 110]}...")
    if achados:
        h = datetime.fromtimestamp(achados[0][0])
        a, b = h - timedelta(minutes=2), h + timedelta(minutes=2)
        print(f'\npara cortar:  py passado.py cortar {ch} {a:%H:%M} {b:%H:%M} "descreva o momento"')


# ------------------------------------------------------------------ cortar
PEDIDO = """Você é editor de cortes de lives de games brasileiras para TikTok e YouTube Shorts.
O usuário pediu um corte de um momento específico da live de {canal}: "{pedido}".
Título da live: "{titulo}"
Transcrição automática (pode ter erros; mistura a voz do streamer com falas do jogo e música), em segundos:

{linhas}

Ache esse momento e monte o melhor corte dele:
- Entre 20 e {max_s} segundos. Comece no mínimo de contexto necessário para entender e termine logo
  depois do ápice/reação principal, sem enrolação no fim. Evite incluir trechos de música tocando.
- GANCHO DE ABERTURA: o vídeo começa com uma prévia de 2 a 4 s do momento mais forte (a reação, o grito,
  a frase marcante) e depois volta pro começo. Informe "gancho_inicio"/"gancho_fim" (dentro do corte) e
  "texto_gancho": frase de até 35 caracteres, maiúsculas, que gere curiosidade sem repetir o título.
- Título chamativo em português, estilo canal de cortes, até 70 caracteres, sem clickbait mentiroso.
- Descrição: 1 a 2 frases contando o que acontece, mencionando {canal} e o jogo, terminando com um
  convite curto ("Segue pra mais cortes!"). Até 220 caracteres, sem hashtags.
- Hashtags: 4 a 6, começando com #.

Responda SOMENTE com JSON:
{{"inicio": 0.0, "fim": 0.0, "gancho_inicio": 0.0, "gancho_fim": 0.0, "texto_gancho": "...", "titulo": "...", "descricao": "...", "hashtags": ["#..."], "motivo": "..."}}"""


def transcrever(arquivo, t0):
    pipe = cortes.load_whisper()
    audio = cortes.read_audio(arquivo)
    res, _ = pipe.transcribe(audio, language="pt", word_timestamps=True, batch_size=8)
    data = {"t0": t0, "dur": len(audio) / 16000, "segs": [], "words": []}
    for s in res:
        if s.text.strip():
            data["segs"].append([round(s.start, 2), round(s.end, 2), s.text.strip()])
            data["words"] += [[round(w.start, 2), round(w.end, 2), w.word.strip()] for w in s.words or []]
    json.dump(data, open(arquivo[:-3] + ".json", "w", encoding="utf-8"), ensure_ascii=False)
    return data


def editar(ch, data, arquivo, pedido, titulo_live=""):
    """Claude escolhe o trecho + gancho + textos; depois monta o vídeo igual aos cortes ao vivo."""
    print("escolhendo o trecho com o Claude...")
    t0 = data["t0"]
    linhas = cortes.fmt_window([dict(data, file=arquivo)], t0)
    c = cortes.ask_claude(PEDIDO.format(canal=ch, pedido=pedido, titulo=titulo_live, linhas=linhas, max_s=90))
    ini = t0 + max(0.0, float(c["inicio"]))
    fim = t0 + min(data["dur"], float(c["fim"]))
    try:
        g0, g1 = t0 + float(c["gancho_inicio"]), t0 + float(c["gancho_fim"])
        c["_gancho"] = (g0, g1) if ini <= g0 < g1 <= fim + 1 and 1.2 <= g1 - g0 <= 5 else None
    except Exception:
        c["_gancho"] = None
    c["nota"] = "pedido"
    print(f"trecho: {fim - ini:.0f}s — {c.get('titulo')}")
    out = cortes.make_clip(ch, ini, fim, c)
    cortes.write_review_page()
    print(f"pronto: {out}")
    return out


def cortar(ch, h_ini, h_fim, pedido):
    a, b = hora(h_ini), hora(h_fim)
    if b - a > 20 * 60 or b <= a:
        sys.exit("Use uma janela de até 20 minutos (início antes do fim).")
    v, segs = vod_for(ch, a)
    pegar = [s for s in segs if s[0] + s[1] > a - 15 and s[0] < b + 15]
    if not pegar:
        sys.exit("A gravação ainda não chegou nesse horário.")
    pasta = os.path.join(PAST, ch)
    os.makedirs(pasta, exist_ok=True)
    nome = os.path.join(pasta, datetime.fromtimestamp(pegar[0][0]).strftime("%Y%m%d-%H%M%S"))
    print(f"baixando {len(pegar)} pedaços de vídeo ({sum(s[1] for s in pegar) / 60:.1f} min)...")
    with cf.ThreadPoolExecutor(8) as ex:
        partes = list(ex.map(lambda s: http(s[2], binary=True), pegar))
    with open(nome + ".ts", "wb") as f:
        for p in partes:
            f.write(p)
    print("transcrevendo...")
    data = transcrever(nome + ".ts", pegar[0][0])
    editar(ch, data, nome + ".ts", pedido, v["titulo"])
    print(f"revise em {os.path.join(cortes.OUT, 'revisar.html')}")


# ------------------------------------------------------------------ pasta de entrada
ENTRADA = os.path.join(cortes.DATA, "entrada")
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".ts", ".webm", ".flv")


def preparar_entrada():
    os.makedirs(os.path.join(ENTRADA, "_feitos"), exist_ok=True)
    for ch in list(cortes.CFG["canais"]) + ["outros"]:
        os.makedirs(os.path.join(ENTRADA, ch), exist_ok=True)
    leia = os.path.join(ENTRADA, "LEIA-ME.txt")
    if not os.path.exists(leia):
        open(leia, "w", encoding="utf-8").write(
            "Coloque o vídeo na pasta do streamer (ou em 'outros').\n"
            "Se quiser dizer qual momento é, crie um .txt com o MESMO nome do vídeo contendo a descrição\n"
            "(ex.: clipe.mp4 + clipe.txt com 'quando ele acha a carta colecionável').\n"
            "O corte pronto aparece na página de revisão; o original vai pra _feitos.\n")


def processar_pasta():
    import shutil
    preparar_entrada()
    feitos = 0
    for ch in sorted(os.listdir(ENTRADA)):
        d = os.path.join(ENTRADA, ch)
        if ch.startswith("_") or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            src = os.path.join(d, f)
            if not f.lower().endswith(VIDEO_EXT):
                continue
            if time.time() - os.path.getmtime(src) < 10:
                continue  # ainda copiando
            print(f"\n[{ch}] processando {f}")
            desc_f = os.path.splitext(src)[0] + ".txt"
            pedido = open(desc_f, encoding="utf-8").read().strip() if os.path.exists(desc_f) else ""
            pedido = pedido or ("o melhor momento deste vídeo (se ele já for curto, use quase inteiro, "
                                "cortando só sobras do começo e do fim)")
            pasta = os.path.join(PAST, ch)
            os.makedirs(pasta, exist_ok=True)
            t0 = float(int(time.time()))
            nome = os.path.join(pasta, datetime.fromtimestamp(t0).strftime("%Y%m%d-%H%M%S"))
            try:
                shutil.copy2(src, nome + ".ts")  # o ffmpeg reconhece o formato pelo conteúdo
                data = transcrever(nome + ".ts", t0)
                if data["dur"] > 20 * 60:
                    raise RuntimeError("vídeo com mais de 20 minutos — corte um pedaço menor")
                editar(ch, data, nome + ".ts", pedido)
                destino = os.path.join(ENTRADA, "_feitos")
            except Exception as e:
                print(f"erro: {e}")
                destino = os.path.join(ENTRADA, "_erro")
                os.makedirs(destino, exist_ok=True)
            for x in (src, desc_f):
                if os.path.exists(x):
                    shutil.move(x, os.path.join(destino, os.path.basename(x)))
            feitos += 1
    return feitos


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "buscar":
        buscar(args[1], args[2:])
    elif len(args) >= 4 and args[0] == "cortar":
        cortar(args[1], args[2], args[3], " ".join(args[4:]) or "o melhor momento desse trecho")
    elif args[:1] == ["pasta"]:
        print(f"{processar_pasta()} vídeo(s) processado(s)")
    elif args[:1] == ["vigiar"]:  # fica olhando a pasta de entrada (iniciado junto com o cortes.py)
        os.makedirs(PAST, exist_ok=True)
        if cortes.already_running():  # trava própria em passado/rodando.pid
            sys.exit(0)
        preparar_entrada()
        while True:
            try:
                processar_pasta()
            except Exception as e:
                print("erro na pasta de entrada:", e)
            time.sleep(15)
    else:
        print(__doc__)
