# FASE 0 — Baseline e instrumentação

Régua fixa medida **antes** de qualquer mudança das FASES 1–3. Sem esta régua, nenhuma
fase seguinte é comprovável. Referência: `PLANO.md`.

## 1. Instrumentação de tokens (implementada)

O caminho de LLM já acumula stats por ação; a FASE 0 acrescentou os campos de **cache** e
expôs o resumo ao frontend.

- `llm_router.py`: `_log_call` / o entry de streaming agora capturam
  `cache_read` / `cache_creation` (via `_cache_tokens`, espelhando o `_usage()` do OP-RPG).
  `get_call_summary()` soma `total_cache_read_tokens` / `total_cache_creation_tokens` e
  expõe os campos por chamada. Cada chamada loga com um `caller` (arquivo:função:linha) que
  serve de label (narrator, detect_mode, journal, crystallize…).
- `routes_game.py` (`/api/game/action`): emite um frame SSE `[USAGE]{json}` antes de
  `[DONE]`, com o resumo do turno.
- Frontend: `api.js` (`onUsage`) → `store.js` (`lastUsage`) → `GameCanvas.jsx` (leitura
  compacta no header: `⛽ Nk↓ Nk↑ [cache] · N calls`, com breakdown por chamada no tooltip).

**Escopo do número exposto.** O `[USAGE]` reflete as chamadas **síncronas** drenadas pelo
stream (detect_mode + narrator + eventual continuação) — a fatia dominante (~90% dos tokens
por turno, o alvo direto das FASES 1–2). As 6 chamadas fire-and-forget
(`_async_side_effects` @989/`_async_world_tick`, `game_session.py:989,1233`) rodam via
`asyncio.create_task` **depois** do resumo e não entram nesse snapshot; cada uma loga
individualmente no servidor. Corrigir a atribuição por turno das chamadas assíncronas
(ex.: `contextvars` herdado pelo `create_task`) fica fora da FASE 0 por ser mudança de
comportamento; anotado como limitação conhecida.

**Número de referência de tokens/turno:** ~133k input por ação (8 chamadas), ~90%
concentrado no `stream_narrative` (histórico de prosa crua). Os campos de cache lêem **0**
até a FASE 2 ligar `cache_control` — é exatamente o ponto: a régua já mede para provar
`cache_read > 0` no turno 2.

## 2. Baseline de tics do narrador

Medido sobre `narrative_c9a42e37.txt` (prosa real do narrador, cenário One Piece / East
Blue), com contraprova em `opening_current.txt` (cenário isekai). Denominador: **61
respostas de narrador** (~59 substantivas), 773 linhas não vazias, ~89 KB.

| Tic | Contagem | Taxa/resposta | Assinatura dominante |
|---|---|---|---|
| **rule_of_three** | 22 | ~0,36 | anáfora "Sem X. Sem Y. Sem Z"; trios de adjetivos; staccato de 3 batidas |
| **npc_recaps** | 16 | ~0,26 | NPC repete literalmente a última fala/ação do jogador antes de reagir ("repete" 11×); correlato "Não é pergunta" 8× |
| **fake_metrics** | 19 | ~0,31 | silêncios cronometrados em segundos exatos; micromovimentos em mm/cm/graus; "exatamente" |
| **em_dashes** | 436 total | ~7,1 bruto | dos 436, ~153 são abertura de diálogo (convenção PT-BR legítima); **~283 interruptivos = o tic real → ~4,6/resposta** |

Notas:
- **npc_recaps é o tic mais estrutural.** Amplificado pela regra "ECO DE FALA DO JOGADOR"
  dos `tone_instructions` do seed (o narrador generalizou o eco da fala do jogador para
  NPCs recapitularem o jogador).
- Os quatro tics já aparecem no `opening_current.txt` (isekai, 52 travessões, triades e
  fake-metrics auto-conscientes) — **não são artefato de um cenário só.**

## 3. Achado paralelo (input para FASE 3a)

As regras anti-tic atuais (`narrator_engine.py:150-152` EN / `182-184` PT-BR;
`opening_generator.py:38-42`) — introduzidas pelo commit `b718ba5` — são anti-exemplos
**pink-elephant**: nomeiam cada vício e citam instâncias literais dele. Pior: os exemplos de
recap são **One Piece-específicos** ("island", "map", "nakama", "Marinha"), vazando cenário
concreto no prompt do engine que deveria ser agnóstico. É o alvo direto da FASE 3a.

## 4. Como reproduzir a contagem de travessão

```
grep -o "—" narrative_c9a42e37.txt | wc -l          # 436 ocorrências
grep -cE "^[[:space:]]*—" narrative_c9a42e37.txt     # 153 aberturas de diálogo
grep -cE "\S" narrative_c9a42e37.txt                 # 773 linhas não vazias
```
