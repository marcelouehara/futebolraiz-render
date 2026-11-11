"""Monta o video vertical do Futebol Raiz Filmes a partir do job enviado pelo n8n (repository_dispatch).

JOB (env, JSON): {"id", "title", "voice", "rate", "hook"?, "emphasis"?, "ambiente"?, "fish": {"reference_id", "model"}?, "scenes": [{"narration", "image_url"}]}
Com "fish" (e o secret FISH_API_KEY), a voz vem do Fish Audio e os tempos das palavras do Whisper;
se o Fish falhar, cai no edge-tts.
Saida: out/<id>.mp4 e out/<id>.json ({"id", "title", "duration"}).
"""
import asyncio
import json
import os
import subprocess
import re
import urllib.request

import edge_tts

W, H, FPS = 1080, 1920, 30
PAD = 0.3  # respiro depois de cada fala
# Grafia so para a pronuncia da voz; na legenda volta ao normal
TROCAS_LEGENDA = {"FIRÚLA": "FIRULA"}
HIGHLIGHT = "&H22C8F0&"  # #F0C822 (amarelo mostarda) em BGR (ASS)
# Retencao: gancho escrito no topo nos primeiros segundos, palavras-chave maiores, som ambiente baixinho
GANCHO_SEG = 3.2
AMBIENTE_SRC = "anoisesrc=color=pink:amplitude=0.8:sample_rate=44100"
AMBIENTE_FILTRO = "highpass=f=350,lowpass=f=1600,tremolo=f=0.25:d=0.5"
AMBIENTE_DB = 24  # quantos dB abaixo da voz

job = json.loads(os.environ["JOB"])
voice = job.get("voice") or "pt-BR-AntonioNeural"
rate = job.get("rate") or "+0%"
os.makedirs("work", exist_ok=True)
os.makedirs("out", exist_ok=True)


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         check=True, capture_output=True, text=True).stdout
    return float(out.strip())


async def tts_voz(text, path, voz):
    words = []
    try:  # edge-tts >= 7.1 precisa pedir WordBoundary explicitamente (antes era o padrao)
        com = edge_tts.Communicate(text, voz, rate=rate, boundary="WordBoundary")
    except TypeError:
        com = edge_tts.Communicate(text, voz, rate=rate)
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7, ch["text"]))
    return words


_whisper = None


def tempos_palavras(path, texto):
    """Fish nao devolve o tempo de cada palavra: o Whisper marca os tempos e o texto do roteiro e mantido."""
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel("base", device="cpu", compute_type="int8")
    # audio decodificado pelo ffmpeg (o PyAV do faster-whisper tem conflito de versao no runner)
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ar", "16000", "-ac", "1", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    segs, _ = _whisper.transcribe(np.frombuffer(pcm, np.float32), language="pt", word_timestamps=True,
                                  initial_prompt=texto)
    ouvidas = [(w.start, w.end) for s in segs for w in (s.words or [])]
    roteiro = texto.split()
    if len(ouvidas) == len(roteiro):
        return [(a, b, w) for (a, b), w in zip(ouvidas, roteiro)]
    # contagem diferente: espalha as palavras do roteiro entre o inicio e o fim da fala, pelo tamanho de cada uma
    ini = ouvidas[0][0] if ouvidas else 0.0
    fim = ouvidas[-1][1] if ouvidas else duration(path)
    pesos = [len(re.sub(r"\W", "", w)) + 1 for w in roteiro]
    total, t, out = sum(pesos), ini, []
    for w, p in zip(roteiro, pesos):
        d = (fim - ini) * p / total
        out.append((t, t + d, w))
        t += d
    return out


def tts_fish(text, path):
    cfg = job["fish"]
    body = json.dumps({"text": text, "reference_id": cfg["reference_id"], "format": "mp3",
                       "prosody": {"speed": cfg.get("speed", 1.0)}}).encode()
    req = urllib.request.Request("https://api.fish.audio/v1/tts", body, {
        "Authorization": "Bearer " + os.environ["FISH_API_KEY"], "Content-Type": "application/json",
        "model": cfg.get("model", "s1")})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())
    return tempos_palavras(path, text)


async def tts(text, path):
    """Tenta a voz pedida e depois vozes reserva; o servico as vezes devolve 'No audio received'."""
    if job.get("fish") and os.environ.get("FISH_API_KEY"):
        for tentativa in range(3):
            try:
                return tts_fish(text, path)
            except Exception as e:  # noqa: BLE001 - qualquer falha do Fish cai no edge-tts
                print(f"fish falhou (tentativa {tentativa + 1}): {e}", flush=True)
                await asyncio.sleep(3)
    erro = None
    reservas = ["pt-BR-AntonioNeural", "pt-BR-ThalitaMultilingualNeural", "pt-BR-FranciscaNeural"] if voice.startswith("pt-") else ["en-US-GuyNeural", "en-US-ChristopherNeural"]
    for voz in [voice] + [v for v in reservas if v != voice]:
        for tentativa in range(3):
            try:
                return await tts_voz(text, path, voz)
            except edge_tts.exceptions.NoAudioReceived as e:
                erro = e
                print(f"sem audio: voz={voz} tentativa={tentativa + 1}", flush=True)
                await asyncio.sleep(2)
    raise erro


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def palavra_legenda(w):
    w = w.replace("{", "(").replace("}", ")").upper()
    for a, b in TROCAS_LEGENDA.items():
        w = w.replace(a, b)
    return w


def chave(w):
    return re.sub(r"\W", "", w).upper()


ENFASE = {chave(w) for w in job.get("emphasis") or [] if chave(w)}


def legenda(words):
    """Grupos de 3 palavras, palavra falada em destaque; nomes, numeros e palavras-chave sempre maiores e coloridos."""
    linhas = []
    grupos = [words[i:i + 3] for i in range(0, len(words), 3)]
    for g, grupo in enumerate(grupos):
        fim_grupo = grupos[g + 1][0][0] if g + 1 < len(grupos) else grupo[-1][1] + 0.4
        for k, (ini, _, _) in enumerate(grupo):
            fim = grupo[k + 1][0] if k + 1 < len(grupo) else fim_grupo
            partes = []
            for j, (_, _, w) in enumerate(grupo):
                txt = palavra_legenda(w)
                if chave(w) in ENFASE or any(c.isdigit() for c in w):
                    txt = f"{{\\fscx118\\fscy118\\c{HIGHLIGHT}}}{txt}{{\\fscx100\\fscy100\\c&HFFFFFF&}}"
                elif j == k:
                    txt = f"{{\\c{HIGHLIGHT}}}{txt}{{\\c&HFFFFFF&}}"
                partes.append(txt)
            pop = "{\\fscx90\\fscy90\\t(0,90,\\fscx100\\fscy100)}" if k == 0 else ""
            linhas.append(f"Dialogue: 0,{ass_time(ini)},{ass_time(fim)},Cap,,0,0,0,,{pop}{' '.join(partes)}")
    return "\n".join(linhas)


def gancho(fim):
    """Frase-gancho fixa no topo enquanto a primeira cena comeca."""
    txt = palavra_legenda((job.get("hook") or "").strip())
    if not txt:
        return ""
    return (f"Dialogue: 1,{ass_time(0)},{ass_time(fim)},Hook,,0,0,0,,"
            f"{{\\fad(0,250)\\fscx70\\fscy70\\t(0,160,\\fscx100\\fscy100)}}{txt}")


clips, todas_palavras, t0 = [], [], 0.0
for i, cena in enumerate(job["scenes"]):
    img, mp3, clip = f"work/{i}.jpg", f"work/{i}.mp3", f"work/{i}.mp4"
    req = urllib.request.Request(cena["image_url"], headers={"User-Agent": "futebolraiz-render"})
    with urllib.request.urlopen(req, timeout=60) as r, open(img, "wb") as f:
        f.write(r.read())
    words = asyncio.run(tts(cena["narration"], mp3))
    d = round(duration(mp3) + PAD, 2)
    frames = int(d * FPS)
    z = f"1+0.12*on/{frames}" if i % 2 == 0 else f"1.12-0.12*on/{frames}"
    vf = (f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
          f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
          f"eq=saturation=0.85:contrast=1.05,colorbalance=rs=0.04:bs=-0.04,noise=alls=10:allf=t,"
          f"fade=t=in:st=0:d=0.25,format=yuv420p")
    run("ffmpeg", "-y", "-i", img, "-i", mp3, "-filter_complex", f"[0:v]{vf}[v];[1:a]apad[a]",
        "-map", "[v]", "-map", "[a]", "-t", str(d), "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", clip)
    real = duration(clip)
    todas_palavras += [(t0 + a, t0 + b, w) for a, b, w in words]
    clips.append(clip)
    t0 += real
    if i == 0:
        fim_cena1 = real

with open("work/list.txt", "w") as f:
    f.writelines(f"file '{os.path.basename(c)}'\n" for c in clips)
run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "work/list.txt", "-c", "copy", "work/all.mp4")

with open("work/subs.ass", "w") as f:
    f.write(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,DejaVu Sans,84,&H00FFFFFF,&H00FFFFFF,&H50000000,&H00000000,-1,0,0,0,100,100,0,0,3,22,0,8,90,90,300,1
Style: Cap,DejaVu Sans,74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{gancho(min(GANCHO_SEG, fim_cena1))}
{legenda(todas_palavras)}
""")

final = f"out/{job['id']}.mp4"


def volume_medio(path):
    err = subprocess.run(["ffmpeg", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))


total = duration("work/all.mp4")
if job.get("ambiente", True):
    run("ffmpeg", "-y", "-f", "lavfi", "-t", f"{total:.2f}", "-i", AMBIENTE_SRC, "-af",
        f"{AMBIENTE_FILTRO},afade=t=in:d=1.5,afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5",
        "-ac", "2", "-ar", "44100", "work/bg.wav")
    ganho = volume_medio("work/all.mp4") - AMBIENTE_DB - volume_medio("work/bg.wav")
    print(f"ambiente: ganho {ganho:.1f} dB", flush=True)
    run("ffmpeg", "-y", "-i", "work/all.mp4", "-i", "work/bg.wav", "-filter_complex",
        f"[0:v]ass=work/subs.ass[v];[1:a]volume={ganho:.1f}dB[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", final)
else:
    run("ffmpeg", "-y", "-i", "work/all.mp4", "-vf", "ass=work/subs.ass", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "21", "-c:a", "copy", "-movflags", "+faststart", final)
meta = {"id": job["id"], "title": job.get("title"), "duration": round(duration(final), 2)}
json.dump(meta, open(f"out/{job['id']}.json", "w"))
print(meta)
