"""Gera descrição (e hashtag do streamer) para cortes que ficaram sem. Uso: py completar_descricoes.py"""
import glob
import json
import os

import cortes

pend = []
for j in sorted(glob.glob(os.path.join(cortes.OUT, "*", "*.json"))):
    d = json.load(open(j, encoding="utf-8"))
    if not d.get("descricao"):
        pend.append((j, d))
print(len(pend), "corte(s) sem descrição")
if pend:
    lista = "\n".join(f'{i}. canal={d["canal"]} | título="{d["titulo"]}" | o que acontece: {d["motivo"]}'
                      for i, (j, d) in enumerate(pend))
    prompt = f"""Para cada corte de live abaixo, escreva uma descrição em português para TikTok/Shorts:
1 a 2 frases contando o que acontece de forma que dê vontade de assistir, mencionando o streamer e o jogo,
terminando com um convite curto (ex.: "Segue pra mais cortes!"). Até 220 caracteres, sem hashtags.
Também sugira 4 a 6 hashtags relevantes.

{lista}

Responda SOMENTE JSON: {{"itens": [{{"n": 0, "descricao": "...", "hashtags": ["#..."]}}]}}"""
    res = cortes.ask_claude(prompt)
    for it in res.get("itens", []):
        j, d = pend[int(it["n"])]
        d["descricao"] = it.get("descricao", "")
        d["hashtags"] = cortes.tags_for(d["canal"], list(d.get("hashtags", [])) + it.get("hashtags", []))
        json.dump(d, open(j, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        open(j[:-5] + ".txt", "w", encoding="utf-8").write(cortes.caption_text(d))
        print("ok:", d["titulo"])
cortes.write_review_page()
print("revisar.html atualizado")
