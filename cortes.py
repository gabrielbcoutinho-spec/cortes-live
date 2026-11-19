"""
Cortes automáticos de lives (Twitch / Kick / YouTube) -> clipes 9:16 legendados.

Fluxo (tudo rodando ao mesmo tempo, por canal):
  gravar (streamlink + ffmpeg, blocos de 60s)
  -> transcrever (faster-whisper na GPU)
  -> escolher momentos (Claude Code headless: `claude -p`, usa a assinatura)
  -> editar (ffmpeg: câmera em cima, jogo embaixo, legenda palavra a palavra)
  -> prontos/<canal>/  +  prontos/revisar.html

Uso:  py cortes.py            (todos os canais do config.json)
      py cortes.py alanzoka   (só os canais listados)
Parar: Ctrl+C
"""
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
DATA = CFG.get("pasta_dados") or ROOT  # gravações e cortes (disco com espaço)
WORK = os.path.join(DATA, "trabalho")
OUT = os.path.join(DATA, "prontos")
PY = sys.executable
CLAUDE = shutil.which("claude")
STOP = threading.Event()
PROCS = set()
PROCS_LOCK = threading.Lock()
LIVE_TITLE = {}
LOG_LOCK = threading.Lock()

os.makedirs(WORK, exist_ok=True)
os.makedirs(OUT, exist_ok=True)


def log(ch, msg):
    line = f"{time.strftime('%H:%M:%S')} [{ch}] {msg}"
    with LOG_LOCK:
        print(line, flush=True)
        with open(os.path.join(WORK, "log.txt"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")


def popen(args, **kw):
    p = subprocess.Popen(args, **kw)
    with PROCS_LOCK:
        PROCS.add(p)
    return p


def free_gb():
    return shutil.disk_usage(DATA).free / 1e9


def seg_time(path):
    """Horário de início do bloco, a partir do nome AAAAMMDD-HHMMSS.ts"""
    return datetime.strptime(os.path.basename(path)[:15], "%Y%m%d-%H%M%S").timestamp()


# ---------------------------------------------------------------- gravação
def check_live(url):
    p = subprocess.run([PY, "-m", "streamlink", "--json", url], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        d = json.loads(p.stdout)
    except Exception:
        return False, None
    if "streams" in d:
        return True, (d.get("metadata") or {}).get("title")
    return False, None


def recorder(ch, c):
    d = os.path.join(WORK, ch)
    os.makedirs(d, exist_ok=True)
    offline_logged = False
    while not STOP.is_set():
        if free_gb() < CFG["min_free_gb"]:
            log(ch, f"pouco espaço em disco ({free_gb():.1f} GB) — gravação pausada")
            STOP.wait(60)
            continue
        try:
            live, title = check_live(c["url"])
        except Exception:
            live, title = False, None
        if not live:
            if not offline_logged:
                log(ch, "offline — verificando a cada 60s")
                offline_logged = True
            STOP.wait(60)
            continue
        offline_logged = False
        LIVE_TITLE[ch] = title or ""
        log(ch, f"AO VIVO — gravando: {title}")
        sl = popen([PY, "-m", "streamlink", "--stdout", c["url"], "1080p60,1080p,720p60,720p,best",
                    "--twitch-disable-ads"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff = popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0",
                    "-c", "copy", "-f", "segment", "-segment_time", str(CFG["segment_seconds"]),
                    "-reset_timestamps", "1", "-strftime", "1",
                    os.path.join(d, "%Y%m%d-%H%M%S.ts")], stdin=sl.stdout, stderr=subprocess.DEVNULL)
        sl.stdout.close()
        while ff.poll() is None and not STOP.is_set():
            time.sleep(2)
        for p in (ff, sl):
            if p.poll() is None:
                p.terminate()
        if not STOP.is_set():
            log(ch, "gravação caiu/terminou — verificando de novo em 10s")
            STOP.wait(10)


def cleaner():
    keep = CFG["retention_minutes"] * 60
    while not STOP.is_set():
        now = time.time()
        for f in glob.glob(os.path.join(WORK, "*", "*.ts")):
            try:
                if now - seg_time(f) > keep:
                    os.remove(f)
                    j = f[:-3] + ".json"
                    if os.path.exists(j):
                        os.remove(j)
            except Exception:
                pass
        STOP.wait(60)


# ---------------------------------------------------------------- transcrição
def load_whisper():
    try:
        import nvidia
        for d in glob.glob(os.path.join(list(nvidia.__path__)[0], "*", "bin")):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
    except Exception:
        pass
    from faster_whisper import BatchedInferencePipeline, WhisperModel
    try:
        m = WhisperModel(CFG["whisper_model"], device="cuda", compute_type="int8_float16")
        dev = "GPU"
    except Exception:
        m = WhisperModel(CFG["whisper_model"], device="cpu", compute_type="int8")
        dev = "CPU"
    log("whisper", f"modelo {CFG['whisper_model']} carregado na {dev}")
    return BatchedInferencePipeline(model=m)


def read_audio(path):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "pipe:1"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def complete_segments():
    out = []
    for d in glob.glob(os.path.join(WORK, "*")):
        if not os.path.isdir(d):
            continue
        files = sorted(glob.glob(os.path.join(d, "*.ts")))
        for i, f in enumerate(files):
            last = i == len(files) - 1
            if last and time.time() - os.path.getmtime(f) < 15:
                continue  # ainda sendo gravado
            if not os.path.exists(f[:-3] + ".json"):
                out.append(f)
    return sorted(out, key=seg_time)  # mais antigo primeiro, todos os canais juntos


def transcriber():
    pipe = load_whisper()
    while not STOP.is_set():
        todo = complete_segments()
        if not todo:
            STOP.wait(3)
            continue
        f = todo[0]
        ch = os.path.basename(os.path.dirname(f))
        try:
            t = time.time()
            audio = read_audio(f)
            dur = len(audio) / 16000
            segs, _ = pipe.transcribe(audio, language="pt", word_timestamps=True, batch_size=8)
            data = {"t0": seg_time(f), "dur": dur, "segs": [], "words": []}
            for s in segs:
                txt = s.text.strip()
                if not txt:
                    continue
                data["segs"].append([round(s.start, 2), round(s.end, 2), txt])
                for w in s.words or []:
                    data["words"].append([round(w.start, 2), round(w.end, 2), w.word.strip()])
            with open(f[:-3] + ".json", "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            atraso = time.time() - (data["t0"] + dur)
            log(ch, f"transcrito {os.path.basename(f)} ({len(data['words'])} palavras, "
                    f"{time.time() - t:.0f}s, atraso {atraso:.0f}s)")
        except Exception as e:
            log(ch, f"erro transcrevendo {f}: {e}")
            with open(f[:-3] + ".json", "w", encoding="utf-8") as fh:
                json.dump({"t0": seg_time(f), "dur": 0, "segs": [], "words": [], "erro": str(e)}, fh)


def load_transcripts(ch):
    out = []
    for j in sorted(glob.glob(os.path.join(WORK, ch, "*.json"))):
        try:
            d = json.load(open(j, encoding="utf-8"))
            if d.get("dur"):
                d["file"] = j[:-5] + ".ts"
                out.append(d)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- escolha dos momentos (Claude)
PROMPT = """Você é editor de cortes virais de lives de games brasileiras para TikTok e YouTube Shorts.
Abaixo está a transcrição automática (pode ter erros) dos últimos minutos da live de {canal}.
Título da live: "{titulo}"
Tempos em segundos desde o início deste trecho.

{linhas}

TAREFA: escolha NO MÁXIMO 1 momento — o melhor deste trecho — e SÓ se ele for forte de verdade.
A resposta normal é {{"cortes": []}}. Um bom canal de cortes posta só o melhor de cada hora de live;
espere achar um corte bom a cada 10–15 minutos, não a cada trecho.

Um corte só vale se tiver TUDO isto:
1. Gancho: algo nos primeiros 3 segundos que faz parar de rolar o feed (grito, frase absurda, situação inusitada).
2. Arco completo: começo, desenvolvimento e um ápice/punchline claro no final.
3. Entende-se sozinho, sem ter visto a live (quem nunca viu o streamer entende e acha graça/se impressiona).
4. Emoção forte e real: gargalhada, raiva, susto, choque, indignação, zoeira afiada, história muito boa.

A graça/emoção tem que vir do STREAMER (a fala, a reação, a decisão dele), não do jogo.
A transcrição mistura a voz do streamer com diálogos de personagens e cutscenes do jogo: fala de NPC,
legenda de missão ou cena roteirizada NÃO conta como momento — só vale se a reação do streamer a ela
for o destaque. Se o "punchline" é uma fala de personagem, a nota máxima é 5.

Evite temas que derrubam alcance ou fazem o vídeo ser removido no TikTok/YouTube: drogas, nudez/sexo,
violência real, preconceito, automutilação, armas reais. Nesses casos nota máxima 5, e nunca use esses
termos no título, na descrição ou no texto do gancho.

NÃO vale (nota máxima 5): comentar gráfico/mapa/carro/clima do jogo, "olha que bonito", narrar o que está
fazendo, explicar mecânica, ler doação/sub/sorteio, propaganda, conversa com chat sem punchline,
reação morna ("caraca", "que isso") sem situação engraçada por trás, trecho que precisa de contexto anterior.
Papo/desabafo/"confissão" falando sobre o jogo ou sobre a vida, sem nada acontecendo na tela, é o tipo de corte
que MAIS decepciona: nota máxima 6, a não ser que seja uma história realmente hilária com punchline forte (aí 8+).
Na dúvida, prefira momentos em que ACONTECE algo (no jogo ou na live) e o streamer reage.
O corte tem que MOSTRAR o acontecimento, não só ele CONTANDO depois. Se o streamer está narrando/relembrando
algo que já passou ("a casa pegou fogo", "eu morri lá atrás", "passei 3h fazendo X") e o fato em si não acontece
dentro do trecho do corte, nota máxima 5. O título só pode prometer o que aparece no próprio vídeo.

Escala de nota (seja duro; a maioria dos trechos não tem nada acima de 6):
- 10: momento que vai viralizar; qualquer pessoa compartilharia.
- 9: muito engraçado/impactante, gancho forte e punchline clara.
- 8: bom e completo, cumpre os 4 critérios sem ressalva.
- 7: bom, com uma pequena ressalva (gancho um pouco mais lento ou punchline mais leve) — ainda vale postar.
- 6 ou menos: falta gancho, punchline ou depende de contexto — não serve.
Também vale (com reação forte do streamer): achar item raro/segredo/easter egg, conquista difícil, falha épica,
momento de "não acredito" no jogo.

Regras:
- Avalie apenas momentos que TERMINAM entre {novo_ini}s e {limite}s (o resto já foi avaliado ou ainda não terminou).
- O corte tem entre {min_s} e {max_s} segundos e termina logo depois do ápice/punchline — sem enrolação no fim.
- O PRIMEIRO SEGUNDO tem que prender: comece já na ação, no grito, na reação ou na frase que puxa a história.
  Nada de começo parado, silêncio, "então, chat..." ou explicação. Use só o mínimo de contexto; se o melhor
  começo exige muito contexto, o momento não serve.
- No "motivo", diga em uma frase qual é o gancho e qual é o punchline.
- TÍTULO NA TELA (aparece grande sobre o vídeo do começo ao fim, igual canal de cortes grande):
  "titulo_tela": a chamada principal, MAIÚSCULAS, até 32 caracteres, citando o streamer pelo apelido
  (ex.: "ALAN NÃO ESPERAVA ESSA CENA NO GTA 6", "GABE FOI HUMILHADO PELO CHAT");
  "subtitulo_tela": complemento curto que dá curiosidade, até 28 caracteres, entre parênteses
  (ex.: "(QUE GEOMETRIA É ESSA?)", "(ELE NÃO SABIA)"). Nada de drogas/sexo/violência no texto.
- Título (pra legenda do post) em português, chamativo, até 70 caracteres, sem clickbait mentiroso.
- Descrição: UMA frase curta (até 120 caracteres) dizendo o que acontece, mencionando {canal}.
  Não repita o título, não escreva em maiúsculas, sem hashtags dentro.
- Hashtags: 2 a 4 relevantes (jogo, tipo de momento), sem espaços, começando com # (a do streamer é adicionada sozinha).

Responda SOMENTE com JSON, sem texto antes ou depois:
{{"cortes": [{{"inicio": 12.0, "fim": 48.5, "titulo_tela": "...", "subtitulo_tela": "(...)", "nota": 8, "titulo": "...", "descricao": "...", "hashtags": ["#..."], "motivo": "..."}}]}}
Se nada valer a pena: {{"cortes": []}}"""

CLAUDE_SEM = threading.Semaphore(CFG.get("claude_parallel", 2))


def ask_claude(prompt):
    if not CLAUDE:
        raise RuntimeError("comando 'claude' não encontrado no PATH")
    with CLAUDE_SEM:
        p = subprocess.run([CLAUDE, "-p", "--model", CFG["claude_model"], "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300, cwd=WORK)
    txt = p.stdout
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b < 0:
        raise RuntimeError("resposta sem JSON: " + (txt + p.stderr)[:300])
    return json.loads(txt[a:b + 1])


def fmt_window(trs, w0):
    lines = []
    for d in trs:
        for s, e, txt in d["segs"]:
            a = d["t0"] + s - w0
            if a >= -1:
                lines.append(f"[{max(a, 0):6.1f}-{d['t0'] + e - w0:6.1f}] {txt}")
    return "\n".join(lines)


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def analyzer(ch, clip_queue):
    last_eval = 0.0
    made = []
    every = CFG["analyze_every_segments"] * CFG["segment_seconds"]
    while not STOP.is_set():
        STOP.wait(10)
        trs = load_transcripts(ch)
        if not trs:
            continue
        latest = max(d["t0"] + d["dur"] for d in trs)
        if last_eval == 0.0:
            last_eval = trs[0]["t0"]
        if latest - last_eval < every:
            continue
        w0 = max(trs[0]["t0"], latest - CFG["context_minutes"] * 60)
        win = [d for d in trs if d["t0"] + d["dur"] > w0]
        linhas = fmt_window(win, w0)
        limite = latest - 8
        novo_ini = max(w0, last_eval - 20)
        last_eval = limite
        if len(linhas) < 40:
            log(ch, "trecho quase sem fala — pulando análise")
            continue
        prompt = PROMPT.format(canal=ch, titulo=LIVE_TITLE.get(ch, ""), linhas=linhas,
                               novo_ini=round(novo_ini - w0), limite=round(limite - w0),
                               min_s=CFG["clip_min_s"], max_s=CFG["clip_max_s"])
        try:
            t = time.time()
            res = ask_claude(prompt)
        except Exception as e:
            log(ch, f"análise falhou: {e}")
            continue
        cortes = res.get("cortes", [])
        log(ch, f"análise ok ({time.time() - t:.0f}s): {len(cortes)} candidato(s)")
        cortes = sorted(cortes, key=lambda c: -float(c.get("nota", 0) or 0))[:1]
        for c in cortes:
            try:
                nota = float(c.get("nota", 0))
                ini, fim = w0 + float(c["inicio"]), w0 + float(c["fim"])
            except Exception:
                continue
            if nota < CFG["canais"].get(ch, {}).get("min_score", CFG["min_score"]) or fim <= ini:
                log(ch, f"  descartado (nota {nota}): {c.get('titulo')}")
                continue
            fim = min(fim, latest)
            if fim - ini < CFG["clip_min_s"]:
                ini = fim - CFG["clip_min_s"]
            if fim - ini > CFG["clip_max_s"]:
                ini = fim - CFG["clip_max_s"]
            if any(overlap(ini, fim, a, b) > 0.4 * (fim - ini) for a, b in made):
                log(ch, f"  repetido, ignorado: {c.get('titulo')}")
                continue
            na_hora = sum(1 for a, b in made if b > fim - 3600)
            if na_hora >= CFG.get("max_cortes_por_hora", 3) and nota < 10:
                log(ch, f"  limite de {na_hora} cortes/hora atingido, ignorado (nota {nota:g}): {c.get('titulo')}")
                continue
            made.append((ini, fim))
            try:  # gancho em tempo absoluto
                g0, g1 = w0 + float(c["gancho_inicio"]), w0 + float(c["gancho_fim"])
                c["_gancho"] = (g0, g1) if ini <= g0 < g1 <= fim + 1 and 1.2 <= g1 - g0 <= 5 else None
            except Exception:
                c["_gancho"] = None
            log(ch, f"  ✂ nota {nota:g} [{fim - ini:.0f}s]: {c.get('titulo')}")
            clip_queue.append((ch, ini, fim, c))


# ---------------------------------------------------------------- edição
def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


LAST_CAM = {}  # canal -> (quando, resultado): última câmera achada


def detect_cam(trs, ini, fim, n=12, ch=None):
    """Acha a câmera do streamer: rosto que aparece parado no mesmo lugar na maioria dos quadros.
    Retorna (crop_x, crop_y, crop_w, crop_h, W, H) em pixels, ou (None, W, H) se não achar."""
    import cv2
    import numpy as np
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    dets, W, H = [], 1920, 1080
    for k in range(n):
        t = ini + (fim - ini) * (k + 0.5) / n
        d = next((d for d in trs if d["t0"] <= t < d["t0"] + d["dur"]), None)
        if not d:
            continue
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t - d['t0']:.2f}", "-i", d["file"],
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                           capture_output=True)
        if not r.stdout:  # quadro não saiu (bloco curto/corrompido): pula
            continue
        try:
            img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
        except cv2.error:
            continue
        if img is None:
            continue
        H, W = img.shape
        # rosto de webcam é pequeno (5%–22% da altura); close de personagem em cutscene é maior
        faces = casc.detectMultiScale(img, 1.1, 4, minSize=(int(H * 0.035),) * 2,
                                      maxSize=(int(H * 0.22),) * 2)
        # webcam fica perto das bordas; rosto no miolo da tela é personagem do jogo
        dets.append([tuple(int(v) for v in f) for f in faces
                     if not (0.28 * W < f[0] + f[2] / 2 < 0.72 * W and 0.2 * H < f[1] + f[3] / 2 < 0.8 * H)])
    def fallback():  # streamer olhou pra baixo/saiu: usa a última posição boa (até 20 min)
        last = LAST_CAM.get(ch)
        if last and time.time() - last[0] < 1200 and last[1][1:] == (W, H):
            return last[1]
        return None, W, H

    if not dets:
        return fallback()
    best = []
    for fl in dets:
        for (x, y, w, h) in fl:
            cx, cy = x + w / 2, y + h / 2
            match = []
            for fl2 in dets:
                m = [f for f in fl2 if abs(f[0] + f[2] / 2 - cx) < 0.5 * w
                     and abs(f[1] + f[3] / 2 - cy) < 0.5 * h and 0.7 < f[2] / w < 1.4]
                if m:
                    match.append(m[0])
            if len(match) > len(best):
                best = match
    if len(best) < max(2, len(dets) * 0.3):
        return fallback()
    fx, fy, fw, fh = (float(np.median([m[i] for m in best])) for i in range(4))
    cw = min(W, fw * 2.7)
    chh = cw / 1.5
    if chh > H:
        chh, cw = H, H * 1.5
    cx, cy = fx + fw / 2, fy + fh * 0.6
    x0 = min(max(0, cx - cw / 2), W - cw)
    y0 = min(max(0, cy - chh / 2), H - chh)
    ev = lambda v: int(v) // 2 * 2
    res = ((ev(x0), ev(y0), ev(cw), ev(chh)), W, H)
    if ch:
        LAST_CAM[ch] = (time.time(), res)
    return res


def wrap_lines(txt, maxc):
    """Quebra em linhas de tamanho parecido (sem palavra sobrando sozinha na última)."""
    n = max(1, -(-len(txt) // maxc))
    alvo = max(len(txt) / n, max((len(w) for w in txt.split()), default=1))
    lines, cur = [], ""
    for w in txt.split():
        if cur and len(cur) + 1 + len(w) > alvo + 3:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def build_ass(words, path, y=725, hook=None, title=None):
    """words: lista (inicio, fim, palavra) já no tempo do clipe.
    hook: (texto, duração, y) — frase de gancho grande no começo do vídeo.
    title: (linha_principal, subtitulo, y_centro, duração) — título fixo na tela o vídeo todo."""
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,7,3,5,40,40,0,1
Style: Hook,Arial Black,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,6,4,5,70,70,0,1
Style: Tit,Arial Black,106,&H0000E6FF,&H0000E6FF,&H00000000,&HA0000000,0,0,0,0,100,100,0,0,1,10,4,5,20,20,0,1
Style: Sub,Arial Black,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,0,0,0,0,100,100,0,0,1,6,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean = []
    for s, e, w in words:
        w = re.sub(r"[{}\\]", "", w).strip(" ,.;:").upper()
        if w:
            clean.append([s, e, w])
    chunks, cur = [], []
    for wd in clean:
        if cur and (len(cur) >= 3 or wd[0] - cur[-1][1] > 0.5 or
                    sum(len(x[2]) for x in cur) + len(wd[2]) > 16):
            chunks.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        chunks.append(cur)
    ev = []
    if title and title[0]:
        main = wrap_lines(re.sub(r"[{}\\]", "", title[0]).strip().upper(), 17)[:3]
        sub = re.sub(r"[{}\\]", "", title[1] or "").strip().upper()
        rows = [("Tit", l, 116) for l in main] + ([("Sub", sub, 86)] if sub else [])
        hgt = sum(r[2] for r in rows)
        yy = title[2] - hgt / 2
        for sty, l, h in rows:
            ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(title[3])},{sty},,0,0,0,,"
                      r"{\pos(540," + str(int(yy + h / 2)) + ")}" + l)
            yy += h
    if hook and hook[0]:
        txt = re.sub(r"[{}\\]", "", hook[0]).strip().upper()
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(hook[1])},Hook,,0,0,0,,"
                  r"{\pos(540," + str(hook[2]) + r")\fad(0,150)\t(0,180,\fscx108\fscy108)\t(180,320,\fscx100\fscy100)}"
                  + txt)
    for ci, ck in enumerate(chunks):
        nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else ck[-1][1] + 0.6
        end_chunk = min(ck[-1][1] + 0.3, nxt)
        for i, wd in enumerate(ck):
            st = wd[0]
            en = ck[i + 1][0] if i + 1 < len(ck) else end_chunk
            if en <= st:
                continue
            parts = []
            for j, x in enumerate(ck):
                if j == i:
                    parts.append(r"{\c&H0000F6FF&\fscx112\fscy112}" + x[2] + r"{\c&H00FFFFFF&\fscx100\fscy100}")
                else:
                    parts.append(x[2])
            ev.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Cap,,0,0,0,,"
                      r"{\pos(540," + str(y) + ")}" + " ".join(parts))
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(ev) + "\n")


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()[:50] or "corte"


def tags_for(ch, tags):
    """Hashtag do streamer sempre primeiro, sem repetir."""
    out, seen = [], set()
    for t in ["#" + ch] + list(tags or []):
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 1 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:5]


def caption_text(d):
    """Texto pronto pra colar (TikTok: tudo junto; YouTube: título no campo de título)."""
    return (f"TÍTULO:\n{d['titulo']}\n\nDESCRIÇÃO:\n{d.get('descricao', '')}\n\n"
            f"{' '.join(d['hashtags'])}\n")


def make_clip(ch, ini, fim, meta):
    # encaixa o começo e o fim em frases completas (não corta ninguém no meio da fala)
    allt = load_transcripts(ch)
    for a, b in [(d["t0"] + s, d["t0"] + e) for d in allt for s, e, _ in d["segs"]]:
        if a < ini < b and ini - a <= 6:
            ini = a
        if a < fim < b and b - fim <= 6:
            fim = b
    for a, b in [(d["t0"] + s, d["t0"] + e) for d in allt for s, e, _ in d["words"]]:
        if a < ini < b:
            ini = a
        if a < fim < b:
            fim = b
    ini -= 0.4
    fim += 0.6
    trs = [d for d in load_transcripts(ch) if d["t0"] + d["dur"] > ini and d["t0"] < fim]
    trs = [d for d in trs if os.path.exists(d["file"])]
    if not trs:
        raise RuntimeError("blocos de vídeo não encontrados (já apagados?)")
    # posição de cada bloco dentro do vídeo concatenado
    cum, pos = 0.0, []
    for d in trs:
        pos.append(cum)
        cum += d["dur"]

    def to_concat(t):
        for d, p in zip(trs, pos):
            if t < d["t0"] + d["dur"]:
                return p + max(0.0, t - d["t0"])
        return cum

    ss = to_concat(ini)
    length = to_concat(fim) - ss

    def words_in(s0, ln):
        out = []
        for d, p in zip(trs, pos):
            for s, e, w in d["words"]:
                a, b = p + s - s0, p + e - s0
                if a >= 0 and b <= ln:
                    out.append((a, b, w))
        return out

    tmp = os.path.join(WORK, "_clip_" + ch)
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "lista.txt"), "w", encoding="utf-8") as f:
        for d in trs:
            f.write("file '" + d["file"].replace("\\", "/") + "'\n")
    cam, W, H = detect_cam(trs, ini, fim, ch=ch)
    if cam:
        cx, cy, cw, chh = cam
        sub_y, hook_y, tit_y = 1500, 880, 720  # título na emenda câmera/jogo, legenda no jogo
        vf_tpl = (f"[0:v]split=2[a][b];"
                  f"[a]crop={cw}:{chh}:{cx}:{cy},scale=1080:720:force_original_aspect_ratio=increase,"
                  f"crop=1080:720,setsar=1[cam];"
                  f"[b]crop=ih*0.9:ih:(iw-ih*0.9)/2:0,scale=1080:1200,setsar=1[game];"
                  f"[cam][game]vstack=inputs=2,fps=30,ass=ASSFILE[v]")
        layout = "camera+jogo"
    else:  # câmera escondida: jogo inteiro no meio, fundo desfocado
        sub_y, hook_y, tit_y = 1420, 330, 440
        vf_tpl = ("[0:v]split=2[a][b];"
                  "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:2,"
                  "eq=brightness=-0.15,setsar=1[bg];"
                  "[b]scale=1080:-2,setsar=1[fg];"
                  "[bg][fg]overlay=0:(H-h)/2,fps=30,ass=ASSFILE[v]")
        layout = "so jogo"
    log(ch, f"layout: {layout}" + (f" (câmera em x={cam[0]} y={cam[1]} de {W}x{H})" if cam else ""))
    quando = datetime.fromtimestamp(ini).strftime("%Y-%m-%d_%Hh%M")
    dest_dir = os.path.join(OUT, ch)
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.join(dest_dir, f"{quando}_{slug(meta.get('titulo', ''))}")

    tela = None
    if CFG.get("titulo_na_tela", True):
        t1 = (meta.get("titulo_tela") or meta.get("titulo") or "").strip()
        t2 = (meta.get("subtitulo_tela") or "").strip()
        if t2 and not t2.startswith("("):
            t2 = f"({t2.strip('()')})"
        tela = (t1, t2) if t1 else None

    def render(s0, ln, ass_name, hook, out):
        build_ass(words_in(s0, ln), os.path.join(tmp, ass_name), y=sub_y, hook=hook,
                  title=(tela[0], tela[1], tit_y, ln) if tela else None)
        # sem fade do preto no começo: o 1º frame já tem que ser o momento
        common = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-ss", f"{s0:.3f}",
                  "-i", "lista.txt", "-t", f"{ln:.3f}", "-filter_complex",
                  vf_tpl.replace("ASSFILE[v]", f"{ass_name},"
                                               f"fade=t=out:st={max(0, ln - 0.35):.2f}:d=0.35[v]"),
                  "-map", "[v]", "-map", "0:a:0",
                  "-af", f"afade=t=in:d=0.04,afade=t=out:st={max(0, ln - 0.35):.2f}:d=0.35"]
        tail = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out]
        cap = ["-maxrate", "5M", "-bufsize", "10M", "-pix_fmt", "yuv420p"]
        r = subprocess.run(common + ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"] + cap + tail,
                           cwd=tmp, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:  # sem NVENC -> CPU
            r = subprocess.run(common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] + cap + tail,
                               cwd=tmp, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-500:])

    texto = (meta.get("texto_gancho") or "").strip() if CFG.get("texto_gancho", False) else ""
    g = meta.get("_gancho")
    total = length
    if g and CFG.get("previa_gancho", False):  # prévia do ápice + corte completo (desligado: ficava estranho)
        gs = to_concat(g[0] - 0.15)
        gl = to_concat(g[1] + 0.25) - gs
        render(gs, gl, "gancho.ass", (texto, gl, hook_y), os.path.join(tmp, "p0.mp4"))
        render(ss, length, "legenda.ass", None, os.path.join(tmp, "p1.mp4"))
        with open(os.path.join(tmp, "partes.txt"), "w") as f:
            f.write("file 'p0.mp4'\nfile 'p1.mp4'\n")
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "partes.txt",
                            "-c", "copy", "-movflags", "+faststart", base + ".mp4"],
                           cwd=tmp, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            raise RuntimeError("juntar gancho: " + r.stderr[-300:])
        total = length + gl
        log(ch, f"  gancho: {gl:.1f}s do ápice no início — \"{texto}\"")
    else:  # sem trecho de gancho: só a frase grande nos primeiros 3 s
        render(ss, length, "legenda.ass", (texto, min(3.0, length), hook_y) if texto else None, base + ".mp4")
    info = {
        "canal": ch, "quando": quando, "duracao": round(total, 1), "layout": layout,
        "texto_gancho": texto, "com_previa": bool(g and CFG.get("previa_gancho", False)),
        "titulo_tela": " / ".join(x for x in (tela or ()) if x),
        "titulo": meta.get("titulo", ""), "descricao": meta.get("descricao", ""),
        "hashtags": tags_for(ch, meta.get("hashtags")),
        "nota": meta.get("nota"), "motivo": meta.get("motivo", ""),
        "video": os.path.relpath(base + ".mp4", OUT).replace("\\", "/"),
    }
    json.dump(info, open(base + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(caption_text(info))
    return base + ".mp4"


def write_review_page():
    items = []
    for j in glob.glob(os.path.join(OUT, "*", "*.json")):
        try:
            items.append(json.load(open(j, encoding="utf-8")))
        except Exception:
            pass
    items.sort(key=lambda d: d["quando"], reverse=True)
    cards = []
    for d in items:
        d["hashtags"] = tags_for(d["canal"], d.get("hashtags"))
        tags = " ".join(d["hashtags"])
        desc = d.get("descricao", "")
        tiktok = html.escape(f"{d['titulo']}\n\n{desc}\n\n{tags}".replace("\n\n\n\n", "\n\n"))
        yt_desc = html.escape(f"{desc}\n\n{tags}".strip())
        cards.append(f"""<div class="c"><video src="{html.escape(d['video'])}" controls preload="metadata"></video>
<div class="m"><b>{html.escape(d['titulo'])}</b><br><span>{html.escape(d['canal'])} · {d['quando']} · {d['duracao']}s · nota {d['nota']}</span>
<p class="d">{html.escape(desc)}</p><p class="t">{html.escape(tags)}</p><p>Motivo: {html.escape(d['motivo'])}</p>
<textarea hidden>{html.escape(d['titulo'])}</textarea><textarea hidden>{yt_desc}</textarea><textarea hidden>{tiktok}</textarea>
<div class="b"><button onclick="cp(this,0)">Título</button><button onclick="cp(this,1)">Descrição + #</button><button onclick="cp(this,2)">Tudo (TikTok)</button></div></div></div>""")
    page = f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><title>Cortes para revisar</title>
<meta http-equiv="refresh" content="60"><style>
body{{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:16px}}
.g{{display:flex;flex-wrap:wrap;gap:16px}}.c{{background:#1c1c1c;border-radius:10px;width:300px;overflow:hidden}}
video{{width:300px;aspect-ratio:9/16;background:#000;display:block}}.m{{padding:10px;font-size:14px}}
span{{color:#999;font-size:12px}}.d{{color:#ddd}}.t{{color:#a78bfa}}
.b{{display:flex;gap:6px}}button{{flex:1;padding:8px 4px;background:#7c3aed;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:12px}}
</style><script>function cp(b,i){{const t=b.closest('.m').querySelectorAll('textarea')[i].value;
navigator.clipboard.writeText(t);const o=b.textContent;b.textContent='Copiado!';setTimeout(()=>b.textContent=o,1200)}}</script>
</head><body><h2>Cortes para revisar ({len(items)})</h2><p>Atualiza sozinho a cada 60s.</p>
<div class="g">{''.join(cards)}</div></body></html>"""
    with open(os.path.join(OUT, "revisar.html"), "w", encoding="utf-8") as f:
        f.write(page)


def editor(clip_queue):
    while not STOP.is_set():
        if not clip_queue:
            STOP.wait(2)
            continue
        ch, ini, fim, meta = clip_queue.pop(0)
        try:
            t = time.time()
            out = make_clip(ch, ini, fim, meta)
            write_review_page()
            log(ch, f"✅ corte pronto ({time.time() - t:.0f}s): {out}")
        except Exception as e:
            log(ch, f"erro editando corte: {e}")


# ---------------------------------------------------------------- main
def already_running():
    """Evita duas cópias gravando a mesma live (blocos duplicados)."""
    lock = os.path.join(WORK, "rodando.pid")
    try:
        pid = int(open(lock).read().strip())
        if pid != os.getpid():
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                               errors="replace")
            if str(pid) in r.stdout and "python" in r.stdout.lower():
                return pid
    except Exception:
        pass
    open(lock, "w").write(str(os.getpid()))
    return None


def main():
    outro = already_running()
    if outro:
        print(f"O programa já está rodando (processo {outro}). Feche a outra janela antes de abrir de novo.")
        return
    canais = CFG["canais"]
    if len(sys.argv) > 1:
        canais = {k: v for k, v in canais.items() if k in sys.argv[1:]}
    if not CLAUDE:
        print("AVISO: comando 'claude' não encontrado — os momentos não serão escolhidos.")
    write_review_page()
    clip_queue = []
    threads = [threading.Thread(target=transcriber, daemon=True),
               threading.Thread(target=cleaner, daemon=True),
               threading.Thread(target=editor, args=(clip_queue,), daemon=True)]
    for ch, c in canais.items():
        threads.append(threading.Thread(target=recorder, args=(ch, c), daemon=True))
        threads.append(threading.Thread(target=analyzer, args=(ch, clip_queue), daemon=True))
    for t in threads:
        t.start()
    # pasta de entrada: vídeos colocados em entrada/<streamer>/ viram corte
    popen([PY, os.path.join(ROOT, "passado.py"), "vigiar"])
    log("sistema", f"rodando para: {', '.join(canais)} — revise em {os.path.join(OUT, 'revisar.html')}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("sistema", "parando...")
        STOP.set()
        with PROCS_LOCK:
            for p in PROCS:
                if p.poll() is None:
                    p.terminate()
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
