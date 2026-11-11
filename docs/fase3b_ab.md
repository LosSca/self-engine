# FASE 3b — A/B controlado (Auditor pós-hoc, Camada 2)

Aferição empírica que fecha o critério "pronto quando" da FASE 3b Camada 2 e decide o
item deixado em aberto do plano: **o Auditor pós-hoc reduz tics/fabricação sem regredir
coerência — e o retorno justifica atacar a Camada 1 (gate na fonte)?**

## Método

Como o Auditor roda **pós-hoc sobre a prosa já pronta**, o A/B é **pareado**: a prosa do
narrador é gerada uma vez (cenário + inputs fixos) e cada resposta passa pelo
`AuditorEngine.audit()`. Cada par (bruto, auditado) difere **só pela auditoria** — zero
variância de amostragem do narrador (mais limpo que a FASE 3a, que variava o prompt).

Harness `backend/scripts/ab_auditor.py`, dois corpora, mesmos 18 inputs, mesmo provider
(**DeepSeek V4 flash** — baseline FASE 0/3a), tanto no narrador quanto no Auditor:

- **stress** — narrador com as regras OLD pink-elephant (prosa tic-densa) → mede **eficácia**.
- **prod** — narrador com as regras NEW de produção (FASE 3a, tic-esparsa) → mede **segurança**.

Medição em duas camadas:
1. **Regex + telemetria** (`ab_auditor.py`) — tics sintáticos, taxa de rewrite, veredito,
   correções por regra, guard de item-tag, queda de `@menções`, latência, falhas de parse.
2. **LLM-judge cego** (workflow `fase3b-blind-judge`, 20 agentes): censo semântico de tics
   nas 72 passagens anonimizadas (raw+audited, braço oculto) + verificação **adversarial e
   com contexto** das reescritas (4 juízes/par, um deles cético caçando dano silencioso, com
   os turnos anteriores como cânone estabelecido). Agregação por `aggregate_3b.py`.

## Resultado

### 1. O Auditor quase nunca age (rewrite ≈ 5,6%)

| | stress | prod |
|---|---|---|
| respostas | 18 | 18 |
| **corrected (reescreveu)** | **1 (5,6%)** | **1 (5,6%)** |
| clean genuíno | 14 | 16 |
| **parse_failed** (degrada p/ original) | **3 (17%)** | **1 (5,6%)** |
| marker_guard rejeitou | 0 | 0 |
| **violação de item-tag** | **0** | **0** |
| queda de `@menção` | 0 | 0 |
| latência média / máx (s) | 28,2 / 54,7 | 14,6 / 39,1 |

O gate default-clean é fortíssimo: mesmo na prosa tic-densa (stress), reescreveu **1 de 18**.
Em **11% dos turnos (4/36)** o audit nem completou — `parse_failed` nas respostas mais longas
(o `final_prose` reescrito inteiro em JSON escapado é frágil no DeepSeek), degradando com
segurança para a prosa original (cobertura zero justo nos turnos mais ricos). As garantias de
segurança se sustentam perfeitamente: **zero** violação de item-tag, **zero** queda de menção.

### 2. Tics agregados quase não se movem

**Regex (sintático, /resp):**

| tic | stress raw→aud | prod raw→aud |
|---|---|---|
| rule_of_three | 0.67 → 0.67 (0%) | 0.17 → 0.17 (0%) |
| fake_metrics | 0.00 → 0.00 | 0.00 → 0.00 |
| em_dash interruptivo | 1.72 → 1.50 (−13%) | 0.39 → 0.39 (0%) |

**LLM-judge cego (semântico, /resp) — a prosa bruta é MUITO mais tic-densa do que o regex vê:**

| tic | stress raw→aud | prod raw→aud |
|---|---|---|
| mechanical_triple | 1.39 → 1.39 (0%) | 1.17 → 1.17 (0%) |
| contrast_by_negation | 1.00 → 1.00 (0%) | 1.56 → 1.56 (0%) |
| gesture_gloss | 0.50 → 0.44 (−11%) | 0.56 → 0.56 (0%) |
| aphorism/oracle closer | 0.50 → 0.44 (−11%) | 0.22 → 0.22 (0%) |
| npc_action_recap | 0.06 → 0.06 | 0.00 → 0.00 |
| pseudo_metric | 0.17 → 0.17 | 0.11 → 0.11 |
| em_dash interruptivo | 1.50 → 1.28 (−15%) | 0.39 → 0.39 (0%) |
| **TOTAL /resp** | **5.11 → 4.78** | **4.00 → 4.00** |

A prosa bruta carrega **~4–5 tics semânticos por resposta** (o juiz pega `mechanical_triple`,
`contrast_by_negation`, `gesture_gloss`, `aphorism` que o regex não vê). O Auditor removeu uma
fração de arredondamento: **todo o delta vem da única reescrita** de cada corpus; os demais
17/18 (stress) e 18/18 (prod) mantêm os tics intactos. Inerte no texture agregado — exatamente
o que o plano previu ("gates curam tics sintaticamente localizáveis mas são inertes em drift
difuso"), e mais forte que o previsto: até os localizáveis passam quase todos.

### 3. As DUAS intervenções foram julgadas PIORES que o original (4/4 cego)

Painel cego, com contexto, adversarial (4 juízes/par):

**p00 — stress idx0 (`em_dash_tic` + `gesture_gloss`).** Remoção cirúrgica correta em forma
(travessões→vírgulas; cortou "…te fixam **como se já soubesse seu nome**" → "…te fixam."),
item-tag intacto, nada inventado. Mas:
- `content_lost_in`: **audited 4/4** — os juízes leram o corte do gloss como **achatamento** de
  uma linha atmosférica de fechamento.
- `cleaner_prose`: equal 3, raw 1 (o audited **não** foi visto como mais limpo).
- **`overall_better`: raw 4/4** — todos preferiram o original.

**p01 — prod idx16 (`player_agency`).** O jogador declarou a eletricidade no turno anterior
(idx15: "deixo a eletricidade formigar nas pontas dos dedos" → narrador rende faíscas azuis,
descarga controlada); no idx16 ("avanço e encerro a luta") o narrador **continua** a habilidade
("mão direita, **ainda** crepitando…"). O Auditor — **cego de contexto por design** (recebe só
`player_input` do turno, nunca o histórico) — leu a eletricidade como poder não declarado e a
**excisou inteira**:
- `established_ability_removed_in`: **audited 4/4** (unânime).
- `content_lost_in`: **audited 4/4**; `same_events`: mostly 4/4.
- **`overall_better`: raw 4/4** — todos preferiram o original; o audit **quebrou a continuidade**.

Duas intervenções em 36 turnos, ambas **net-negativas** por painel unânime: uma regressão de
gosto (achatou uma linha) e uma quebra de continuidade (apagou habilidade estabelecida).

## Nuances / limitações honestas

- **N de reescritas = 2.** A direção é unânime (4/4 nos dois), mas "o Auditor faz dano quando
  age" é **sinal forte, não prova**; a taxa de rewrite de 5,6% exigiria ~180 respostas para ~10
  reescritas. O que **é** robusto independente de N: (a) a inércia agregada (censo de 72), e (b)
  a **cegueira de contexto da regra de agência é arquitetural** — `audit()` recebe só o
  `player_input` do turno, então a falha do p01 é sistêmica, não azar de amostra; o par foi só
  uma instância viva dela.
- **Provider/cenário específicos.** Auditor em DeepSeek V4 flash, clone de One Piece, pt-br. Um
  modelo mais forte (ex. Opus) poderia editar melhor/mais conservador. O prompt é agnóstico, mas
  a qualidade do julgamento depende do modelo — e em produção o Auditor roda no provider da campanha.
- **O p00 é parcialmente subjetivo:** "como se já soubesse seu nome" é justamente o `gesture_gloss`
  que a régua mira, mas os juízes o acharam evocativo. Mostra a tensão real: régua de tic é
  heurística; remoção mecânica achata boa prosa. Argumento a favor de um gate menos rombudo.
- **parse_failed em 11%** é telemetria, não julgamento — mas significa cobertura zero nos turnos
  mais longos (os mais propensos a tic).

## Conclusão e decisão sobre a Camada 1

O A/B era pra **decidir empiricamente se a Camada 1 (gate na fonte) vale o custo**. Decide — e
redireciona:

1. **A Camada 1 NÃO se justifica como redutor de tic.** A FASE 3a (regras afirmativas na fonte)
   já capturou o ganho: o corpus prod (regras NEW) tem em_dash 0,39/resp vs stress 1,72 (−77%) e
   rule_of_three 0,17 vs 0,67 (−75%). O resíduo é o que é; um gate reflexivo no ponto de geração é
   otimização de segunda ordem sobre um ganho de primeira ordem já realizado — por um custo de
   reescrever o núcleo validado (forced tool-call no router + re-integrar o cache de 4 zonas).

2. **Mais urgente que a Camada 1: a Camada 2 embarcada tem modos de dano demonstrados.** Nas duas
   vezes que agiu, piorou a prosa (4/4). O mais grave e **arquiteturalmente certo** é a
   **cegueira de contexto da regra de agência**, que apaga silenciosamente habilidades/itens que o
   jogador estabeleceu em turnos anteriores. A ação com respaldo empírico é **corrigir/escopar a
   Camada 2 antes** de cogitar a Camada 1:
   - dar contexto recente à regra de agência (a **janela de cena aberta** da FASE 1 já é exatamente
     esse recorte) para o teto virar "input + cena estabelecida", não "input do turno isolado"; ou
     restringir a agência a fala/decisão **nova** (não continuação de habilidade); ou
   - subir a barra de confiança do rewrite (as duas edições passaram apesar do "na dúvida, limpo").
   - a fragilidade de parse em prosa longa deixa 11% dos turnos sem cobertura.

Ou seja: a hipótese "gate cura tic" **não se confirma** para o pós-hoc, e o gate na fonte ataca um
problema que a FASE 3a já resolveu. O trabalho de maior valor e menor risco não é a Camada 1 — é
tornar a Camada 2 **context-aware na agência** (ou desarmá-la onde ela não tem contexto pra julgar).

## Correção — Auditor context-aware (concluída)

Em vez da Camada 1, a correção porta o padrão de **contexto total** do Auditor do OP-RPG
(`../one-piece-rpg/backend/app/pipeline/auditor.py`), agnóstico de cenário. A raiz do bug era
arquitetural: `_audit_narrative` passava só o `player_input` do turno. Agora o Auditor recebe:

- **RECENT SCENE** — os turnos anteriores (a janela de cena aberta da FASE 1), rotulados
  "continuidade física imediata; referência, nunca fonte de extração". Isso ELEVA o teto de
  agência: continuar uma habilidade/item/postura que a cena estabeleceu é carry-over legítimo.
- **WORLD CONTEXT** — memória cristalizada + cards + inventário + ficha do jogador + NPCs
  (o mesmo bundle que o narrador vê), para um cross-check de **consistência**.

Mudanças: `AuditorEngine.audit()` ganha `recent_scene`/`world_context`; `game_session`
(`_render_recent_scene`, `_build_audit_world_context`) monta e passa o contexto. O prompt
(EN+PT, workflow de 3 rascunhos → crítica adversarial → síntese) reescreve **AGÊNCIA** (teto =
input + cena estabelecida; fiscaliza só o que o narrador **inventou** além disso) e adiciona a
régua **CONTINUIDADE / CONSISTÊNCIA** (`world_contradiction`, barra alta, só contradição
concreta e apontável dos dois lados; mudança que a própria passagem faz não é contradição).

**Re-validação (mesma prosa bruta do A/B, Auditor velho → novo; isola a mudança do Auditor):**

| caso | BEFORE (context-blind) | AFTER (context-aware) | veredito do painel |
|---|---|---|---|
| **prod idx16** (alvo) | corrected → **excisou** a eletricidade | **clean** — eletricidade preservada (markers 4→4), robusto 2/2 | bug **corrigido** |
| stress idx0 | corrected (painel: original melhor) | **clean** — parou o rewrite net-negativo | alinhado ao painel |
| stress idx10 | parse_failed | corrected `world_contradiction` (narrador disse "amanhã" vs "em dois dias" estabelecido) | **4/4: fix correto, AFTER melhor** |
| stress idx13 | clean | corrected `player_agency` — **over-reach** | **4/4: falso positivo** (NPC propondo plano ≠ agência) |
| parse_failed | 4/36 | **1/36** | — |

**Achados verificados:** o bug de cegueira de contexto está **resolvido** (idx16 preserva a
habilidade estabelecida; 2/2 estável). O Auditor ganhou uma capacidade **real e confirmada** de
continuidade (idx10, 4/4). **Residual honesto:** trocou o falso positivo arquitetural por um
mais raro — over-reach de agência quando um NPC toma iniciativa/propõe um plano (idx13, 4/4
confirmado sem violação). Um reforço no prompt (NPC-iniciativa ≠ agência do jogador) reduziu para
~2/3 clean, mas não zerou: é limitação de capacidade do **modelo auditor** (DeepSeek erra ~1/3
numa prosa ambígua; os juízes Opus acertam 4/4) — não da régua. Net fortemente positivo: um dano
arquitetural corrigido + capacidade de continuidade nova, ao custo de um over-reach raro e
model-dependent. Não recebe patch de card/state (o Lunar não tem essa arquitetura); a autoridade
segue sendo reescrita de prosa, agora informada.

## Reproduzir

```
cd backend
CORPUS=both PYTHONPATH=. python scripts/ab_auditor.py     # BEFORE: gera docs/fase3b_ab/{stress,prod}/… + summary.json
python scripts/prep_judge_3b.py                            # anonimiza p/ juiz cego (resp/*.txt, verify/*/)
# workflow fase3b-blind-judge  -> docs/fase3b_ab/judge_output.json
python scripts/aggregate_3b.py ../docs/fase3b_ab/judge_output.json
CORPUS=both PYTHONPATH=. python scripts/revalidate_3b.py   # AFTER: re-audita o mesmo corpus com o Auditor context-aware
# workflow fase3b-after-verify -> adjudica as reescritas novas (idx10, idx13)
```
