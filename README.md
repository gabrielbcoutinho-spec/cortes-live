# Cortes ao vivo

Grava lives da Twitch/Kick, acha os melhores momentos sozinho e gera cortes verticais (9:16) prontos pra TikTok / YouTube Shorts:

- câmera do streamer em cima, jogo embaixo (a câmera é achada automaticamente, mesmo se o streamer mudar ela de lugar)
- título grande fixo na tela + legenda palavra por palavra
- título, descrição curta e hashtags pra colar no post
- página `prontos/revisar.html` pra assistir e escolher o que postar (a postagem é manual)

## Como funciona

1. `streamlink` grava cada live em blocos de 60 s.
2. `faster-whisper` transcreve o áudio (usa a GPU NVIDIA se tiver).
3. A cada 5 minutos, o Claude (`claude -p`, usando a assinatura do Claude Code, sem API paga) lê a transcrição e escolhe no máximo 1 momento bom. Só vira corte se a nota for 8 ou mais, com no máximo 3 cortes por hora por canal.
4. O `ffmpeg` monta o vídeo vertical.

## Requisitos

- Windows, com Python 3.12 (`py`)
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) no PATH
- [Claude Code](https://claude.com/claude-code) instalado e logado (o comando `claude` precisa funcionar no terminal)
- Placa NVIDIA é recomendada (GTX 1650 já dá conta). Sem ela funciona, só que mais devagar.

```
py -m pip install -r requirements.txt
```

## Configuração (`config.json`)

- `pasta_dados`: onde ficam as gravações e os cortes. **Troque pra uma pasta do seu PC** (ex.: `"D:\\cortes-live"`), ou deixe `""` pra usar a própria pasta do projeto. Precisa de uns 10 GB livres.
- `canais`: os canais a gravar, no formato `"nome": {"url": "https://kick.com/nome"}`. Também funciona com links da Twitch.
- `min_score`: nota mínima pra virar corte (8 = só momentos fortes).
- `max_cortes_por_hora`: limite por canal.
- `titulo_na_tela`: liga ou desliga o título grande no vídeo.

No `iniciar.bat`, troque o caminho `J:\cortes-live\prontos\revisar.html` pela sua `pasta_dados`.

## Uso

- **Ao vivo:** dê dois cliques no `iniciar.bat`. Os cortes vão aparecendo em `<pasta_dados>\prontos\revisar.html`.
- **Pasta de entrada:** jogue qualquer vídeo em `<pasta_dados>\entrada\<canal>\` e ele vira corte no mesmo formato. Um `.txt` com o mesmo nome do vídeo serve pra descrever o momento.
- **Momentos que já passaram (só Kick):**
  ```
  py passado.py buscar gabepeixe carta colecionavel
  py passado.py cortar gabepeixe 15:30 15:36 "ele acha a carta rara"
  ```
  O `buscar` procura palavras na legenda automática do VOD. O `cortar` baixa só o pedaço do horário pedido e monta o corte.

## Aviso

Os cortes usam lives de outras pessoas. Peça autorização ao streamer antes de postar.
