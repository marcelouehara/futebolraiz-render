"""Acrescenta o video renderizado ao historico.csv (memoria de temas lida pelo n8n). Uso: python3 historico.py <csv>"""
import csv
import datetime
import json
import os
import sys

job = json.loads(os.environ["JOB"])
meta = json.load(open(f"out/{job['id']}.json"))
path = sys.argv[1]
novo = not os.path.exists(path)
with open(path, "a", newline="") as f:
    w = csv.writer(f)
    if novo:
        w.writerow(["data", "id", "pagina", "titulo", "fonte", "duracao", "video"])
    w.writerow([datetime.date.today().isoformat(), job["id"], job.get("pagina", ""), job.get("title", ""),
                job.get("fonte", ""), meta["duration"],
                f"https://marcelouehara.github.io/{os.environ.get('GITHUB_REPOSITORY', '/futebolraiz-render').split('/')[1]}/{job['id']}.mp4"])
