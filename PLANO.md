# PLANO.md — Transferência OP-RPG → Project Lunar

Plano de implementação para portar ao Project Lunar os padrões comprovados da engine
irmã **One Piece RPG** (`../one-piece-rpg`), com prioridade em **método de cache** e
**qualidade narrativa**. Todos os mecanismos são agnósticos de cenário; nenhum conteúdo
de One Piece é transferido.

O fio condutor: **nunca realimentar prosa crua do próprio narrador como histórico.** Isso é
ao mesmo tempo caro (é a maior fatia de tokens por turno) e nocivo à qualidade (ensina o
modelo a imitar os próprios tiques — auto-condicionamento em contexto). A solução ataca os
dois problemas com o mesmo movimento: crystals destilados cobrem o passado, e só a **cena
aberta** entra como prosa crua.

---

## Decisões tomadas

1. **Cache (FASE 2): adota o cloaking do OP-RPG.** Instruções vão dentro da primeira
   mensagem `user`, entre tags `<narrator-instructions>…</narrator-instructions>`, com
   `cache_control` nos blocos de conteúdo. Elimina a incerteza de como o CLIProxyAPI
   (OAuth, porta 8318) trata o parâmetro `system=`. O prefixo cloaked **é** o prefixo
   cacheado.
2. **Qualidade (FASE 3b): adota a arquitetura completa do OP-RPG** — gate `pre_emit_audit`
   na fonte (narrador migra para tool-call) **e** Auditor pós-hoc como rede final. Os dois
   em conjunto, como na engine irmã.

---

## Diagnóstico do estado atual

Achados que orientam o plano (confirmados por leitura direta do código):

- **Metade da arquitetura-alvo já existe no Lunar, desligada.** `narrator_engine.py` tem
  `build_system_prompt_parts()` (split estático/dinâmico, linhas 263-321) e
  `complete_single_call()` com `cache_control: ephemeral` (490-542) — ambos são **código
  morto**, porque `game_session._is_single_call_provider()` retorna `False` incondicional
  (game_session.py:691-698). O caminho vivo (`stream_narrative`, :415) manda **string
  monolítica sem cache**.
- **Redundância que consome ~90% dos tokens por turno.** Os mesmos eventos entram no prompt
  duas vezes: como prosa crua no `history_slice` (até 600 mensagens em janela de 1M —
  narrator_engine.py:361-374) **e** como JSON destilado nos crystals (bloco WORLD MEMORY).
  O `_dynamic_history_slice` (:376-413) desconhece o `_last_crystal_cursor`
  (memory_engine.py:224,297); nunca solta a prosa de cenas já crystallizadas.
- **Infra de crystals já funciona** — pirâmide SHORT→MEDIUM→LONG→MEMORY
  (memory_engine.py:50-77), schema fact-preserving (102-145), gatilho
  `AUTO_CRYSTALLIZE_THRESHOLD = 4` (:207). Só não substitui a prosa crua.
- **Combate a tics = anti-padrão pink-elephant.** As `_NARRATOR_RULES`
  (narrator_engine.py:149-152 EN / 181-184 PT) e o `opening_generator.py:38-42` nomeiam
  cada vício e dão **exemplos literais** dele ("AVOID the obsessive 'rule of three'"
  seguido de três exemplos de rule-of-three). É exatamente o que o experimento
  `narrator_determinism_prompt` do OP-RPG mediu: remover um anti-exemplo derrubou a
  fabricação de 50%→11%. Não há regex nem pós-processamento — é 100% proibição textual.
- **Narrador é texto puro**, sem tool schema (`stream_narrative`). Terreno limpo para o
  gate estruturado. O `_SINGLE_CALL_FORMAT` (:466-488) é um andaime JSON pronto e ocioso.
- **Transporte:** produção fala com CLIProxyAPI em `127.0.0.1:8318` (`.env:13`), o mesmo
  binário do OP-RPG, com seção `cloak` configurável (`proxy/cliproxyapi/config.example.yaml`
  :227-238). No caminho com proxy o `stream()` já cai para `acompletion(stream=False)`
  (llm_router.py:383) — **o streaming ao cliente hoje é ilusório**, o que barateia a
  migração do narrador para saída estruturada.
- **Sem guard de temperature por modelo.** `llm_router.py` sempre envia `temperature`
  (333/352/386/416) — dá 400 em Opus/Sonnet/Fable 5.
- **Config por-cenário:** `language` / `tone_instructions` / `lore_text` vivem na dataclass
  `Scenario` (scenario_store.py:18-30), não em `Campaign`. Story cards têm `created_at`
  (scenario_store.py:33-40) — chave estável para ordenação append-only.

---

## FASE 0 — Instrumentação e baseline
**Habilitador · risco nulo.** Sem medir, nenhuma fase seguinte é comprovável. O OP-RPG é
empírico por design.

**Mudanças**
- `llm_router.py`: capturar `usage` de cada resposta e expor
  `cache_read_input_tokens` / `cache_creation_input_tokens` / `input` / `output`
  (espelhar `_usage()` do OP-RPG em `proxy/client.py:47-57`). Logar por chamada com um
  `label` (narrator, detect_mode, journal, crystallize…).
- Somar tokens por **ação do jogador** (as ~8 chamadas) num contador de turno, exposto no
  devtools do frontend.
- Capturar 20-30 saídas reais do narrador num cenário fixo como **baseline de tics**:
  contagem de rule-of-three, recaps de NPC, métrica falsa, travessão.

**Validação / pronto quando:** existe um número "tokens/turno" e uma taxa de tics/resposta
_antes_ de qualquer mudança. É a régua das FASES 1-3.

---

## FASE 1 — Cortar a realimentação de prosa crua
**Maior retorno isolado · ataca token E tic.**

**Objetivo.** O narrador passa a ver: crystals (passado destilado, já no prompt) + **janela
curta de prosa só da cena aberta**. Elimina os ~90% de tokens do `stream_narrative` e o
auto-condicionamento (o modelo para de reler os próprios tics como se fossem cânone de
estilo).

**Diagnóstico (hoje).** `_dynamic_history_slice` (narrator_engine.py:376-413) corta por nº
de mensagens e budget de tokens, nunca pela fronteira de crystallização. O
`_last_crystal_cursor` (memory_engine.py:224,297) marca o último evento destilado — a
fronteira "cena fechada vs. aberta" que ninguém consome.

**Mudanças**
1. `_handle_narrative` (game_session.py:~1048): montar o `history` do narrador apenas com
   eventos `created_at > _last_crystal_cursor`, mais um **buffer de overlap** de 1 batch
   (não abrir buraco entre a última prosa e o último crystal).
2. `_dynamic_history_slice`: aceitar a janela já recortada; manter o piso de 4 mensagens
   (:411) como rede de coerência de curtíssimo prazo.
3. `_rebuild_history_from_events` (game_session.py:135-153): aplicar o mesmo corte por
   cursor no boot, para o comportamento pós-restart bater com o de sessão viva.
4. **Invariante de cobertura contígua:** nada sai da janela sem já estar num crystal. Se a
   crystallização assíncrona ainda não fechou o intervalo, manter a prosa até ela rodar.

**Risco/Mitigação.** Perda de detalhe fino de médio prazo. O schema de crystal já é
fact-preserving (memory_engine.py:102-145); ajustar o overlap (1-2 batches) e validar
contra o baseline. Reversível por flag.

**Validação / pronto quando:** tokens/turno do `stream_narrative` caem de ~133k para dezenas
de k; history_slice típico ≈ cena aberta (unidades de mensagens); rule-of-three/recap caem
mesmo antes da FASE 3; sem regressão de coerência no baseline.

---

## FASE 2 — Cache em 4 zonas (cloaking)
**Multiplica a economia da FASE 1.** Pré-requisito: FASE 1 (a janela de prosa precisa ficar
_fora_ do prefixo cacheado — cache é prefix-dependent).

**Objetivo.** Transformar o prefixo estático (instruções + catálogos) em cache read a partir
do 2º turno.

**Mudanças**
1. **Split de 2→4 zonas** (evoluir `build_system_prompt_parts`, :263):
   - **Zona 0 (cache):** papel + idioma + `TONE AND STYLE` + `_OPENING_CANON_HEADER` +
     `_build_narrator_rules`. Byte-idêntico por cenário.
   - **Zona 1 (cache):** catálogos near-static — subconjunto **estável** de story cards +
     crystals tier MEMORY (canon imutável). **Ordenar por `(created_at, id)`**
     (append-only, `StoryCard.created_at` scenario_store.py:33-40) para que um card novo só
     anexe e re-caceie o delta.
   - **Zona 2 (volátil, sem cache):** crystals recentes, inventário, NPCs, hints, grafo +
     **diretiva de idioma** (um prefixo cacheado serve PT e EN — padrão
     `language.py:46-47` / `_EN_DIRECTIVE` do OP-RPG).
   - **Zona 3 (dinâmica):** janela de prosa da cena aberta + `player_input`.
2. **Cloaking:** portar `build_content()` do OP-RPG (`proxy/client.py:110-142`) — instruções
   na 1ª mensagem `user` com tags, `cache_control` (`{"type":"ephemeral","ttl":"1h"}`) nos
   blocos das zonas 0 e 1, header `extended-cache-ttl-2025-04-11` (client.py:63-64) para
   sobreviver ao think-time.
3. **Emitir blocos com `cache_control` no caminho de streaming** (portar o padrão de
   `complete_single_call:525-540` para `stream_narrative`).
4. **Guard de temperature por modelo** no `llm_router.py`: portar `_NO_SAMPLING_MODELS` +
   `_accepts_temperature` (client.py:69-73); omitir `temperature` para Opus/Sonnet/Fable 5
   (hoje enviado em 333/352/386/416).

**Risco/Mitigação.** Byte-identidade frágil — qualquer reordenação ou timestamp no prefixo
estoura o cache. Render determinístico, catálogos ordenados por chave estável, telemetria da
FASE 0 confirmando `cache_read > 0` no turno 2. Se o proxy não repassar o header de TTL,
cair para cache efêmero de 5 min (GA, sem header).

**Validação / pronto quando:** `cache_read_input_tokens > 0` do 2º turno em diante; custo/turno
cai na proporção do prefixo (referência OP-RPG: 134k→34k); nenhum 400 de sampling; PT e EN
compartilham o mesmo prefixo cacheado.

---

## FASE 3a — Despink-elephant
**Quick win · risco baixo · retorno já medido no OP-RPG.** Independente das FASES 1-2 (pode
ir junto com a 0).

**Mudanças.** Reescrever as regras anti-tic de **proibição+exemplo** para **princípio
afirmativo curto**, removendo os anti-exemplos literais:
- `_NARRATOR_RULES` EN (narrator_engine.py:149-152) e PT-BR (:181-184): trocar "AVOID the
  obsessive 'rule of three' [três exemplos]" por afirmação de cadência variada (pares,
  frases longas, trio ocasional) — sem citar o vício. Recap → "NPCs reagem à intenção com
  uma pergunta, um gesto ou um silêncio". Métrica falsa → "observação sensorial
  qualitativa".
- `opening_generator.py:38-42`: mesmo tratamento.
- **Manter** as regras afirmativas que já funcionam: render do input em cena (:141/:173),
  nomes completos, coerência de inventário/MEMORY.

**Validação / pronto quando:** A/B cego contra o baseline da FASE 0; queda de tic/fabricação
ao _remover_ os anti-exemplos (expectativa pela medição do OP-RPG).

---

## FASE 3b — Gate `pre_emit_audit` + Auditor
**Refinamento · mais invasivo.** Como o streaming via proxy já é ilusório
(llm_router.py:383), o custo real é baixo. Adota a arquitetura completa do OP-RPG (as duas
camadas).

**Camada 1 — gate na fonte (Rota A).** Migrar o narrador para tool-call, estendendo o
`_SINGLE_CALL_FORMAT` (:466-488) com um campo `pre_emit_audit`: enum de compromissos
afirmativos preenchidos **antes** do `narrative_text` e **descartados no parse** (padrão
`agents.py:64-124` do OP-RPG; descarte em `agents.py:238-241`). Reativar o caminho
single-call (`_is_single_call_provider`, game_session.py:691-698). Autocondicionamento local:
o modelo reafirma as regras no ponto de geração.

**Camada 2 — Auditor pós-hoc (Rota B).** Gate LLM final sobre a prosa antes de revelar ao
jogador: default limpo, cirúrgico, best-effort com timeout (~90s no OP-RPG, `AUDIT_TIMEOUT_S`
em config), libera a prosa original se estourar. Referência: reveal-after-audit em
`runner.py:~2922` do OP-RPG.

**Nota empírica (OP-RPG).** Gates curam tics sintaticamente localizáveis (travessão,
glosa/contraste) mas são **inertes** em drift difuso de registro/fabricação — por isso 3a
(despink) tem retorno maior e vem antes. 3b é otimização de segunda ordem.

**Fora de escopo (recomendação, não código):** reescrever os `tone_instructions` dos seeds
(ex. bloco "REGRAS ABSOLUTAS" com "NÃO/NUNCA" do `scenario-sword-god.json`) de proibição
para style anchor afirmativo — é conteúdo do usuário.

---

## Ordem e dependências

```
FASE 0 (instrumentação)  ──►  habilita medir tudo
   │
   ├──► FASE 3a (despink)      ← quick win independente, pode ir junto com a 0
   │
   └──► FASE 1 (janela curta)  ← maior retorno; pré-requisito da 2
              │
              └──► FASE 2 (cache 4 zonas, cloaking)  ← multiplica a economia
                          │
                          └──► FASE 3b (gate pre_emit_audit + Auditor)  ← só depois
```

---

## Referência: padrões-fonte no OP-RPG (`../one-piece-rpg`)

- **Cache / cloaking:** `backend/app/proxy/client.py` — `build_content()` (110-142, 4 blocos
  cloaked, 2 breakpoints), `_CACHE_CONTROL` (63), `_CACHE_HEADERS` (64),
  `_NO_SAMPLING_MODELS` + `_accepts_temperature` (69-73), `_usage()` (47-57).
- **Split estável/volátil + append-only:** `backend/app/pipeline/director.py` — catálogo
  ordenado por `(created_at, id)` e stable-vs-dynamic (1473-1530).
- **Idioma no bloco volátil:** `backend/app/pipeline/language.py` — `output_directive()`
  (46-47), `_EN_DIRECTIVE` (35-43).
- **Gate `pre_emit_audit`:** `backend/app/pipeline/agents.py` — schema (64-124), descarte no
  parse (238-241).
- **Root-cause (prosa crua como histórico):** `backend/app/pipeline/runner.py` (60-64);
  reveal-after-audit (~2922).
- **Auditor / timeout:** `backend/app/config.py` (`AUDIT_TIMEOUT_S`).

> Estado: **FASE 0, 3a, 1, 2 e 3b (Camada 2) concluídas** (baseline em `docs/fase0_baseline.md`).
> A **Camada 2 (Auditor pós-hoc)** está no ar; a **Camada 1 (gate na fonte via tool-call)** foi
> deliberadamente diferida, porque no estado atual do código ela exigiria reescrever o caminho de
> geração (streaming + cache de 4 zonas via SDK anthropic direto) para forced tool-call no router
> — alto risco sobre uma "otimização de segunda ordem". O **A/B do Auditor** (abaixo, concluído)
> resolveu a questão: **a Camada 1 não se justifica como redutor de tic** (a FASE 3a já capturou o
> ganho na fonte), e a Camada 2 pós-hoc é inerte no agregado + fez dano nas duas vezes que agiu. O
> próximo trabalho de código não é a Camada 1, mas **corrigir a cegueira de contexto da Camada 2**.

## FASE 0 — concluída

Deltas em relação ao plano original:
- **Metade da instrumentação já existia** (`_call_log`/`reset_call_log`/`get_call_summary`/
  `_log_call` no `llm_router.py`; `reset`+`summary` já chamados em `routes_game.py`). A FASE 0
  acrescentou: captura de `cache_read`/`cache_creation` (`_cache_tokens`, espelhando `_usage()`
  do OP-RPG), somatório no `get_call_summary()`, frame SSE `[USAGE]{json}` antes de `[DONE]`, e
  o readout de devtools no header (`api.js` `onUsage` → `store.js` `lastUsage` → `GameCanvas`).
- **Baseline de tics medido** (61 respostas): rule_of_three 22 (~0,36/resp), npc_recaps 16
  (~0,26/resp, o mais estrutural), fake_metrics 19 (~0,31/resp), travessão 436 total → ~283
  interruptivos (~4,6/resp). Detalhe e metodologia em `docs/fase0_baseline.md`.
- **Limitação conhecida:** o `[USAGE]` cobre as chamadas síncronas (~90% dos tokens); as 6
  fire-and-forget correm com o resumo e ficam fora do snapshot (logam individualmente).
  Atribuição por turno das assíncronas fica para depois (mudança de comportamento).
- **Confirmado para a FASE 3a:** os anti-tics do commit `b718ba5` são pink-elephant e vazam
  conteúdo One Piece-específico no prompt do engine agnóstico.

## FASE 3a — concluída

Reescrita dos três loci de anti-tic de **proibição+exemplo** (pink-elephant) para **princípio
afirmativo curto**, confirmados como os únicos no código-fonte por varredura
(`narrator_engine.py` EN + PT-BR, `opening_generator.py`). Nenhum outro `CRITICAL` era
anti-tic estilístico — os demais são regras de coerência/canon/knowledge-boundary e ficaram
intactos.

Deltas:
- **Header despinkado.** `ANTI-CLICHÉ / STYLISTIC VARIATION (CRITICAL — these tics break
  immersion)` / `ANTI-VÍCIOS DE LINGUAGEM` → `PROSE TEXTURE` / `TEXTURA DA PROSA`. O próprio
  header antigo nomeava o vício.
- **Três intents afirmados sem nomear o vício nem citar anti-exemplo:** (A) NPC demonstra
  compreensão agindo pela intenção (pergunta/gesto/silêncio/releitura) em vez de ecoar as
  ações; (B) cadência segue o sentido do momento; (C) sensação ancorada em percepção
  qualitativa. Removidos os anti-exemplos literais de rule-of-three, recap e pseudo-métrica.
- **Vazamento de franquia eliminado.** O anti-exemplo PT-BR carregava termos One Piece
  (`nakama`, `Marinha`, `ilha`) dentro do engine agnóstico — removidos.
- **Pink-elephant de segunda ordem corrigido na síntese:** a regra de cadência estava
  estruturada como um trio (o próprio vício que ela pede pra afrouxar); recomposta como
  contraste de duas opções. Anglicismo `agindo sobre` → `agindo a partir de`.
- **Regras afirmativas que já funcionavam foram mantidas** (render do input em cena, nomes
  completos, coerência de INVENTORY/MEMORY).

Método: workflow de 3 rascunhos independentes → crítica adversarial por candidato
(vice-naming, anti-exemplo, framing negativo, vazamento de franquia, paralelismo EN/PT-BR,
brace-safety) → síntese da melhor versão corrigindo todos os flags.

Validação estrutural: `py_compile` OK; `_build_narrator_rules` renderiza EN e PT-BR sem erro
(o `.format()` sobrevive — zero chaves órfãs); asserts confirmam headers novos, ausência dos
termos de franquia e dos nomes de vício.

**A/B controlado cego (concluído):** harness (`backend/scripts/tic_harness.py`) gera prosa nos
dois braços — regras OLD (pink-elephant) vs NEW (afirmativas) — com cenário/inputs/provider
(DeepSeek V4 flash) fixos, variando só o bloco de regras. 18 respostas/braço. Medição em duas
camadas: regex determinístico + LLM-judge cego (workflow, 36 juízes + verificação adversarial
de cada recap). **Todos os tics caem no braço NEW:** rule_of_three −78% (regex) / −32%
(judge); fake_metrics −100%/−50%; em_dash interruptivo −100%; npc_recaps confirmado −50%.
Hipótese confirmada, sem regressão. Detalhe, tabelas e limitações em `docs/fase3a_ab.md`
(dados brutos em `docs/fase3a_ab/`).

## FASE 1 — concluída

O narrador deixa de reler a prosa crua da campanha inteira; passa a ver só a **cena aberta**
(eventos após a última fronteira de crystal) + 1 batch de overlap. O passado destilado já vive
nos crystals do system prompt.

Deltas em relação ao plano original:
- **`_history` permanece íntegro; o recorte é derivado, não destrutivo.** Em vez de mutilar
  `self._history` (itens 1/3 do plano), o corte vive num método novo `_open_scene_history()`
  (`game_session.py`), consumido só nos dois caminhos que alimentam o narrador (`stream_narrative`
  e o single-call ocioso, mais as continuations de truncamento). Os ~20 consumidores de cauda de
  `_history` (power eval, RAG query, meta, `_history[-20:]`, scans reversos) ficam intactos. O
  item 3 ("boot bate com sessão viva") é satisfeito **por construção**: a janela deriva do
  `_history` completo, então boot e sessão viva produzem fatias idênticas sem tocar
  `_rebuild_history_from_events`.
- **Fronteira por cursor de crystal, não por contagem de mensagens.** `overlap_cursor =
  short[-2].source_end_created_at` (penúltimo SHORT crystal); `n = |eventos PLAYER_ACTION +
  NARRATOR_RESPONSE após o cursor|` via `get_after`; janela = `_history[-n:]`, com piso de 4 msgs.
  O alinhamento 1:1 entre `_history` e o store se sustenta (PLAYER_ACTION é persistido no início
  de `process_action`, então até rejeições de anti-griefing casam 2 msgs ↔ 2 eventos). O design
  **sempre erra para incluir mais prosa** (no ponto de chamada o `_history` fica no máximo 1
  evento atrás do store), nunca cortando prosa não-crystallizada.
- **Reversível por flag** `LUNAR_FEATURE_OPEN_SCENE_WINDOW` (default ON); off restaura o feed de
  history completo. Fallback defensivo: qualquer exceção, backlog patológico (`n ≥ 4000`) ou
  campanha ainda curta (`< 2` SHORT crystals) retorna o history completo.

Correção de raiz motivada por review adversarial (4 lentes + cético por achado):
- 1 defeito real sobreviveu: **cursor-jump no rebuild.** `_rebuild_memory_crystals` reconstruía
  `source_end_created_at` a partir do `created_at` do evento MEMORY_CRYSTAL (**timestamp de
  persistência**), não do último evento raw. Como a crystallização é fire-and-forget e roda por
  último, eventos do turno seguinte podem ter `created_at` **antes** desse persist-time; no boot o
  cursor pulava por cima deles e eles nunca mais eram crystallizados (gap permanente na memória
  destilada). A FASE 1 tornava esse gap latente visível ao narrador.
- **Corrigido na fonte:** `_persist_crystal` (`memory_engine.py`) passa a gravar
  `source_start/source_end_created_at` reais no payload; `_rebuild_memory_crystals` os restaura
  (fallback para `ev.created_at` em crystals legados). Cursor reconstruído == cursor vivo.
  Beneficia também a crystallização normal (fim do gap), não só a janela.

Validação:
- `py_compile` OK nos dois módulos. 12 testes novos em `tests/services/test_open_scene_window.py`:
  invariante de cobertura, overlap por cursor, piso de 4 msgs, fallbacks (flag off, sem crystal,
  cursor ausente, backlog, exceção) e **regressão do cursor-jump** (reproduz persist-skew +
  restart; confirmado que falha sem a correção → cursor vira o persist-time).
- **Economia medida:** história ao narrador cai de **173.560 → 2.935 tokens (−98,3%)** num cenário
  de 300 trocas (janela de 1M), batendo o alvo do plano (baseline ~133k de história crua → dezenas
  de k; aqui ~3k, dentro do system prompt que permanece).
- Sem regressão: as 5 falhas de `test_game_session.py` são pré-existentes (mocks async não
  configurados), idênticas no baseline por `git stash`.

Método: implementação direta + workflow de verificação adversarial (lentes: invariante,
alinhamento history/store, call-sites/consumidores, overlap/edge-cases; cada achado re-verificado
por cético independente com viés a REFUTAR o benigno "inclui mais prosa"). 1 achado confirmado,
corrigido na raiz.

## FASE 2 — concluída

O system prompt do narrador vira 4 zonas; o prefixo estável (papel/tom/regras/opening + catálogo
canônico) vira **cache read** a partir do 2º turno via cloaking no caminho Anthropic. Reversível
por flag `LUNAR_FEATURE_PROMPT_CACHE` (default ON); OFF restaura o `build_system_prompt` monolítico.

Deltas em relação ao plano:
- **Transform por provider no `llm_router`, não no narrador.** O narrador (`stream_narrative_cached`)
  emite o system como blocos de texto com `cache_control` nas zonas 0 e 1 (2 de 4 breakpoints) e
  permanece agnóstico de provider. O `llm_router._prepare_cached_messages` detecta a "forma cacheada"
  (system com `content` = lista de blocos) e aplica o transporte: **Anthropic** faz cloaking (blocos
  na 1ª mensagem `user`, tag `<narrator-instructions>` no bloco 0, `cache_control` preservado, header
  beta `extended-cache-ttl-2025-04-11`, TTL 1h); **DeepSeek/OpenAI** achata as zonas num único system
  string (auto-cacheia por prefixo). Gating porque `cache_control` é da Anthropic e o Lunar usa
  litellm com provider selecionável por request.
- **Zonas.** Z0 (cache) = papel+idioma+character_setup+`TONE AND STYLE`+opening+regras — byte-idêntico
  por campanha. Z1 (cache) = LORE cards ordenados por `(created_at,id)` + crystals tier MEMORY
  (`render_permanent_context`) — canon append-only. Z2 (volátil) = memory recente (sem permanent) +
  inventário + NPC + journal + hints + grafo + RAG de cards **não-LORE** + diretiva de comprimento.
  Z3 = janela de cena aberta (FASE 1) + `player_input`.
- **Guard de temperature em TODAS as chamadas** (não só narrador): `_NO_SAMPLING_MODELS` =
  opus-4-8/4-7, sonnet-5, fable-5, mythos-5 → omite `temperature` (400 se enviado). Opus 4.6 /
  Sonnet 4.6 seguem aceitando. Isto **habilita** modelos Anthropic modernos, que hoje davam 400.
- **Split de memory e cards é aditivo** (defaults preservam comportamento): `include_permanent` em
  `build_context_window`/`_async`; `exclude_lore` em `_format_story_cards_context`. Nenhum consumidor
  fora do caminho de narrativa cacheada muda.
- **Desvio do plano:** "um prefixo cacheado serve PT e EN" não se aplica — as regras do Lunar são
  específicas por idioma (`_build_narrator_rules` EN vs PT-BR) e cada campanha tem idioma fixo, então
  Z0 é específico por idioma mas byte-estável por campanha. Sem diretiva de idioma separada em Z2.

Correções de raiz motivadas pela review adversarial (workflow: 5 lentes → achar → cético por achado;
**14 achados, 5 confirmados, 9 refutados** — todos os refutados eram telemetria-dependente ou caminhos
latentes/mortos: single-call desabilitado, sem fallback configurado, nenhum caller passa `extra_headers`,
mínimo de 4096 tokens é comportamento inerente auto-corrigível):
1. **`max_tokens` (slider por-request) vazava no Z0 cacheado** via `_length_instruction` (embute o
   número literal de tokens no bloco de regras) → mudar o slider estourava o prefixo. Corrigido: a
   diretiva de comprimento saiu do Z0 (`build_zone0` usa `include_length=False`) e virou linha volátil
   em Z2 (`length_directive`). Z0 agora é `max_tokens`-independente.
2. **Z1 despejava LORE sem budget** → risco de overflow em janela pequena + cenário lore-heavy.
   Corrigido: cap estável determinístico (oldest-first por `(created_at,id)`, generoso em 1M, ≥1 card),
   preservando append-only.
3. **Header "WORLD LORE" vazio em Z2** quando todos os cards são LORE → `_format_story_cards_context`
   retorna `""` quando o filtro esvazia a seleção.
4. **Cloak rodava antes do `_sanitize_messages_for_anthropic`** → reordenado (sanitize antes do
   transform), restaurando o strip de assistant líder legado no caminho cloaked.

Validação:
- `py_compile` OK nos 4 módulos; **147 testes passam**. As 6 falhas + 8 erros restantes são
  pré-existentes (mocks async, neo4j, versão do FastAPI) — reprodução idêntica com a flag OFF confirma
  zero regressão nova. 2 testes de narrativa atualizados para mockar o caminho cacheado (novo default).
- **Conservação de conteúdo** (teste dedicado): cada fato do monolito antigo aparece **exatamente uma
  vez** na união das zonas, na zona correta (permanent+LORE→Z1, recent+NPC→Z2, estático→Z0). Zero
  duplicação, zero perda.
- **Byte-estabilidade** (teste dedicado): Z0 determinístico e `max_tokens`-independente; Z1 independe da
  ordem das linhas do DB (o sort normaliza) e é **append-only** (novo card mantém o prefixo anterior
  idêntico → Z0 segue cache read, só o delta de Z1 re-caceia). Telemetria da FASE 0 lê a forma
  cacheada/cloaked sem erro.

**Limitação conhecida (resolvida pela validação empírica abaixo).** O OP-RPG provou o cloaking com o SDK
`anthropic` direto; o Lunar usa **litellm**. Se o litellm não repassar `cache_control`/`extra_headers` ao
CLIProxyAPI, `cache_read` fica 0 — confirmar num turno real Anthropic (alvo do plano:
`cache_read_input_tokens > 0` do 2º turno). Fallback: flag OFF. Plano B: mover o narrador Anthropic pro
SDK `anthropic` direto. O mínimo de 4096 tokens (Opus) para cachear Z0+Z1 pode não disparar em cenários
mínimos (inerente, auto-corrige quando tom/opening/LORE crescem).

## FASE 2 — validação empírica (concluída)

Turnos reais contra Claude Max via CLIProxyAPI na `:8318` (o proxy vivo é a instância do OP-RPG, mesmo
binário + mesmo backend Claude Max; usada com a api-key dela — o transporte é idêntico ao do Lunar).
Harness em `backend/scripts/`: `validate_fase2_cache.py` (caminho real do narrador via litellm),
`direct_cache_probe.py` (payload cloaked idêntico direto no proxy), `litellm_wire_capture.py` (corpo HTTP
que o litellm de fato envia).

**Dois achados independentes, ambos conclusivos:**

1. **O mecanismo de cloaking está correto e cacheia de verdade.** O `direct_cache_probe` reproduz o payload
   byte-idêntico que o Lunar monta (blocos cloaked no 1º `user`, `cache_control ttl 1h` em Z0/Z1, header
   `extended-cache-ttl-2025-04-11`) e posta direto no proxy, lendo o usage cru: **turno 1
   `cache_creation_input_tokens=16643` com `ephemeral_1h_input_tokens=16643`** (o TTL de 1h sobrevive ao
   proxy — não cai pro efêmero de 5 min); **turno 2 `cache_read_input_tokens=16643`, `input_tokens=119`**.
   Prefixo Z0+Z1 escrito no turno 1 e lido inteiro no turno 2. Sonnet 4.6 cacheia; Opus 4.6 **não** cacheou
   um prefixo de 1373 tokens (< mínimo 4096 do Opus) — confirma empiricamente a limitação de min-tokens.

2. **O caminho de produção (litellm 1.43.0) NÃO cacheia — `cache_read=0` é real, não cegueira de
   telemetria.** O `litellm_wire_capture` mostra que o litellm **remove todo `cache_control`** antes de
   enviar ao proxy: 0 ocorrências no corpo, blocos com `cache_control=None`. No caminho cloaked strippa os
   markers dos blocos de mensagem; no caminho `system` **achata a lista de blocos numa string**
   (`payload.system` vira `str`), descartando o marker. O `extra_headers`/beta-header **passa** (está no
   fio), mas sem `cache_control` a Anthropic não cacheia. Sintoma colateral: o litellm devolve usage
   degenerado (`{prompt_tokens: 68}`, sem campos de cache), então a telemetria `[USAGE]` da FASE 0 também
   lê 0.

**Conclusão.** A FASE 2 está **estruturalmente correta e comprovadamente eficaz** — mas **inerte em
produção** enquanto o narrador Anthropic passar pelo litellm 1.43.0, que não repassa `cache_control` por
nenhum caminho (nem mensagem, nem `system`). Nenhuma reorganização de zonas resolve. Remédios, em ordem de
esforço: **(a)** subir o litellm para uma versão que preserve `cache_control` de content-block no adapter
Anthropic e revalidar com o mesmo harness; **(b) Plano B** — rotear o narrador Anthropic pelo SDK
`anthropic` direto (o `direct_cache_probe` já prova que funciona ponta-a-ponta). Até então: DeepSeek segue
sem cache_control (auto-cache por prefixo, não afetado) e o flag `LUNAR_FEATURE_PROMPT_CACHE` pode ficar ON
sem dano (o cloaking é montado mas o litellm o descarta — comportamento idêntico a OFF no provider
Anthropic).

### Correção aplicada — Plano B (SDK `anthropic` direto)

Escolhido o Plano B. `llm_router.py`: quando o provider é ANTHROPIC **e** as mensagens carregam
`cache_control` (a forma cloaked da FASE 2), `complete`/`stream` roteiam pelo SDK `anthropic` direto ao
proxy (`_complete_anthropic_sdk` + `_get_anthropic_client`; `AsyncAnthropic` cacheado por `(base_url, key)`,
`max_retries=0` com o mesmo loop de retry `_PROXY_RETRY_DELAYS`). O `cache_control` e o header beta são
preservados; a resposta do SDK é remapeada pra forma litellm (`_SDKResponse`) pra `_log_call`/`_cache_tokens`
lerem o usage real. Detecção via `_has_cache_control`. Demais chamadas (DeepSeek, e chamadas Anthropic sem
cache_control como detect_mode) seguem no litellm — mudança cirúrgica. Dependência nova: `anthropic==0.116.0`
(httpx 0.27 / pydantic intactos, sem conflito com litellm 1.43.0).

**Re-validação (mesmo harness, caminho real do router):** turno 1 `cache_creation=15290` (zonas Z0+Z1
escritas) + `cache_read=1359` (prefixo do proxy) → turno 2 `cache_read=16649` (prefixo inteiro servido do
cache), `input_tokens=207`. `cache_read_input_tokens > 0` do 2º turno **atingido em produção**, e a
telemetria `[USAGE]` da FASE 0 agora lê os números reais (o adapter expõe input/cache_read/cache_creation).
`test_llm_router.py` 5/5; suíte sem regressão nova (as falhas restantes são pré-existentes de outras partes:
contagem de journal e drift de mock `process_tick(language=)`, nada de anthropic/cache).

## FASE 3b — concluída (Camada 2; Camada 1 diferida)

Adotada a **Camada 2 (Auditor pós-hoc)** — rede final sobre a prosa antes do reveal. A **Camada 1 (gate
na fonte via tool-call)** foi **deliberadamente diferida**: a decisão travada original ("as duas camadas,
como no OP-RPG") foi tomada antes de a FASE 2 recolocar o caminho vivo em streaming + cache de 4 zonas via
SDK anthropic direto. Reativar o single-call / migrar o narrador para `emit_turn` hoje significaria portar
forced tool-call ao `llm_router` (que só tem `complete`/`stream`) e re-integrar o cache das 4 zonas no
formato tool-call — reescrita de alto risco sobre o núcleo já validado, por um retorno que o próprio plano
classifica como "otimização de segunda ordem". Fica como fase futura, orientada pelo A/B do Auditor.

Deltas em relação ao plano:
- **Novo `AuditorEngine`** (`backend/app/engines/auditor_engine.py`): `audit(prose, player_input, language,
  tone_instructions, max_tokens) → (final_prose, report)`. Uma chamada `complete()` que retorna JSON
  (`verdict` clean|corrected, `corrections[]`, `final_prose`, `reasoning_summary` + um gate reflexivo
  `pre_emit_audit` **descartado no parse**). Sem tool-call — usa o `complete()` que já existe. Default clean:
  em parse-fail, exceção, `final_prose` vazio, `verdict != corrected` ou prosa idêntica, devolve a prosa
  **original**.
- **Adaptação vs OP-RPG.** O Auditor do OP-RPG é enorme (cards/presence/mint/merge) porque assenta numa
  arquitetura de cards/agentes que o Lunar não tem. Aqui é enxuto: a régua cobre só **FORMA** (os tics da
  FASE 3a, sintaticamente localizáveis), **AGÊNCIA** do jogador e **IDIOMA** da campanha. Não recebe estado
  do mundo/lore/inventário — o gate fica barato e escopado, sem julgar canon/continuidade.
- **Ponto de inserção único.** Em `_handle_narrative`, após a auto-continuação + limpeza e **antes** do
  `yield full_response`. A prosa auditada é a que o jogador vê **e** a que flui downstream (extração de
  tags, `_history`, event store, crystallização, witnesses) — sem divergência entre revelado e persistido.
  Só NARRATIVE/COMBAT; META é excluído. Combate também é auditado (`_handle_combat → _handle_narrative`).
- **Preservação de marcadores.** No reveal a prosa ainda carrega `[ITEM_ADD|USE|LOSE:...]` e `@nomes`. Guard
  **hard** nas tags de item (fingerprint por **evento parseado**, mesma gramática de `_extract_inventory_tags`,
  order-independent): reescrita que dropa/adiciona/altera qualquer evento de inventário é **rejeitada** →
  revela a original. `@nomes` são cosméticos: queda é **logada**, nunca rejeita (uma excisão de agência que
  corta a frase com a única menção ainda vale).
- **Prompt desenhado por workflow** (3 rascunhos independentes → crítica adversarial por candidato →
  síntese): EN + PT-BR, byte-idêntico por idioma. Zero pink-elephant (vícios nomeados **abstratamente**, as
  assinaturas de detecção — "as if"/"como quem" — marcadas "nunca escreva você mesmo"; zero travessão
  demonstrado). Zero vazamento de cenário. `rule_violated` num vocabulário **fixo em inglês** nos dois
  idiomas (identificadores estáveis para a telemetria de tics). `TONE AND STYLE` entra como **contexto**, não
  gatilho de correção.
- **Flags.** `LUNAR_FEATURE_NARRATOR_AUDIT` (default ON, espelha as flags das outras fases) e
  `LUNAR_AUDIT_TIMEOUT_S` (default 90s, **parse defensivo** — valor inválido degrada pro default, nunca
  crasha o import). Best-effort: `asyncio.wait_for` no ponto de chamada; qualquer timeout/erro revela a
  prosa intacta. A chamada passa pelo mesmo router → aparece no `[USAGE]` da FASE 0.

Correções de raiz motivadas pela review adversarial (workflow: 5 dimensões → achar → cético por achado; **5
achados confirmados, corrigidos; refutados descartados** — o "pink-elephant" das assinaturas de detecção e o
"final_prose truncado" foram refutados como já tratados):
1. **Combate FAIL/CRIT_FAIL vazava o teto de agência.** `_handle_combat` embrulha o input do jogador num
   diretivo `[SYSTEM: dice FAILS…]` e o passa como `player_input`; o Auditor recebia o wrapper como "raw
   player input". Corrigido: `raw_player_input` separado ao longo de `_handle_narrative`; o teto de agência é
   sempre a linha crua.
2. **Regex do guard dessincronizava do parser de `[ITEM_ADD]`.** `[^\]]+` parava no primeiro `]`, então uma
   tag ADD com `]` no nome/categoria podia ter `category`/`source` alterados sem o guard pegar. Corrigido:
   fingerprint pela mesma gramática de 3 regexes do `_extract_inventory_tags`.
3. **`LUNAR_AUDIT_TIMEOUT_S` mal-formado crashava o backend no import** (`float("")` → `ValueError`, único
   ponto onde o Auditor podia derrubar o processo). Corrigido: `_audit_timeout_s()` com fallback pro default
   e clamp positivo/finito.
4. **Conjugação PT-BR:** `poli` (pretérito de "polir", "eu poli") não concordava com "Você" → `lustra`.

Validação:
- `py_compile` OK; **24 testes novos** (`tests/engines/test_auditor_engine.py` 13 + `tests/services/test_narrator_audit.py`
  11): clean pass-through, rewrite aplicado, fallback em parse-fail/exceção/timeout, guard rejeita/aceita
  (inclui regressão do `]`-no-nome), descarte do `pre_emit_audit`, headroom de `max_tokens`, roteamento de
  idioma, flag on/off, parse defensivo do timeout. **Zero regressão nova** — os testes que falham (mocks
  async/neo4j/FastAPI) são subconjunto do baseline por `git stash -u`.
- **Validação end-to-end real** (harness `backend/scripts/validate_fase3b_auditor.py`, DeepSeek V4 via
  proxy): prosa limpa passa intacta (`clean`); violação de agência é **corrigida por excisão** (narrador
  inventou fala + decisão do jogador → prosa cortada ao teto declarado, `rule_violated: player_agency`); tag
  de item **preservada verbatim** sob o guard. Invariante de tag mantido em todos os casos.

Método: scouting inline → workflow de design do prompt (3 rascunhos → crítica adversarial → síntese) →
implementação direta → workflow de review adversarial (5 dimensões, cético por achado) → correção dos 5
confirmados → re-validação estrutural + end-to-end real.

## FASE 3b — validação A/B do Auditor (concluída)

A/B pareado do Auditor pós-hoc (Camada 2), mesmo método da FASE 3a. Como o Auditor roda sobre a prosa
já pronta, o par (bruto, auditado) difere só pela auditoria — zero variância do narrador. Dois corpora
(stress = regras OLD tic-densas; prod = regras NEW de produção), 18 inputs, DeepSeek V4 flash no narrador
e no Auditor. Harness `backend/scripts/ab_auditor.py` + `prep_judge_3b.py` + `aggregate_3b.py`; judge cego
via workflow `fase3b-blind-judge` (20 agentes: censo semântico de 72 passagens + verificação adversarial
com contexto das reescritas). Detalhe e tabelas em `docs/fase3b_ab.md`.

Achados (todos verificados):
- **Rewrite ≈ 5,6%** (1/18 em cada corpus). Default-clean fortíssimo: mesmo na prosa tic-densa, reescreveu
  1 de 18. **11% dos turnos (4/36) deram `parse_failed`** nas respostas mais longas (o `final_prose` inteiro
  em JSON escapado é frágil no DeepSeek) — degrada com segurança, mas cobertura zero nos turnos mais ricos.
- **Inerte no agregado.** A prosa bruta carrega ~4–5 tics semânticos/resposta (censo cego); o Auditor removeu
  só o delta das duas reescritas. TOTAL stress 5,11→4,78; prod 4,00→4,00. Segurança perfeita: 0 violação de
  item-tag, 0 queda de menção.
- **As 2 intervenções foram julgadas PIORES que o original (4/4 cego, com contexto).** Uma foi regressão de
  gosto (achatou uma linha ao cortar um `gesture_gloss`); a outra, **quebra de continuidade**: o Auditor,
  **cego de contexto por design** (`_audit_narrative` passa só o `player_input` do turno, nunca o histórico),
  excisou uma habilidade (eletricidade) que o jogador estabelecera no turno anterior — falso positivo de
  agência **arquitetural**, não azar de amostra.

Veredito: a hipótese "gate cura tic" **não se confirma** para o pós-hoc; a Camada 1 (gate na fonte) ataca um
problema que a FASE 3a já resolveu na fonte (prod tem em_dash −77% e rule_of_three −75% vs stress). O trabalho
de maior valor/menor risco **não** é a Camada 1 — é **dar contexto recente à regra de agência da Camada 2**
(a janela de cena aberta da FASE 1 é exatamente esse recorte) para o teto virar "input + cena estabelecida"
em vez de "input do turno isolado", ou escopá-la a fala/decisão nova. Limitação: N de reescritas = 2 (direção
unânime, mas amostra pequena); a inércia agregada e a cegueira de contexto são robustas independente de N.

### Correção aplicada — Auditor context-aware (porta o padrão do OP-RPG)

Em vez da Camada 1, portado o **contexto total** do Auditor do OP-RPG, agnóstico de cenário. `AuditorEngine.audit()`
recebe `recent_scene` (janela de cena aberta da FASE 1 — continuidade) + `world_context` (memória+cards+inventário+
ficha+NPCs); `game_session` (`_render_recent_scene`, `_build_audit_world_context`) monta e passa. O prompt (EN+PT,
workflow 3 rascunhos → crítica → síntese) reescreve AGÊNCIA (teto = input + cena estabelecida; fiscaliza só o que o
narrador **inventou** além disso; NPC-iniciativa ≠ agência) e adiciona a régua CONTINUIDADE/CONSISTÊNCIA
(`world_contradiction`, barra alta). `_PRE_EMIT_KEYS` +2 chaves.

Re-validação na mesma prosa bruta (Auditor velho → novo, isola a mudança), adversarialmente verificada:
**bug-alvo corrigido** — prod idx16 preserva a eletricidade estabelecida (excisão → clean, markers 4→4, robusto 2/2);
**capacidade nova confirmada** — stress idx10 pega uma contradição de timeline (4/4 correto); parou o rewrite
net-negativo do idx0; parse_failed 4/36 → 1/36. **Residual honesto:** um over-reach de agência mais raro quando um
NPC propõe plano (idx13, 4/4 falso positivo); reforço no prompt reduz a ~2/3 clean, resíduo é model-dependent
(DeepSeek; juízes Opus 4/4 corretos). Net fortemente positivo. 30/30 testes do Auditor passam. Detalhe em
`docs/fase3b_ab.md` (seção "Correção — Auditor context-aware").
