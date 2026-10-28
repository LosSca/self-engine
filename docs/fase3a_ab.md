# FASE 3a — A/B controlado (despink)

Aferição empírica que fecha o critério "pronto quando" da FASE 3a: **queda de tic ao
remover os anti-exemplos pink-elephant**. É um A/B **controlado e cego**, mais rigoroso que
comparar contra a sessão-baseline da FASE 0 (que tinha N variáveis).

## Método

Harness (`backend/scripts/tic_harness.py`) gera prosa de narrador nos **dois braços** com
tudo idêntico exceto o bloco de regras anti-tic:

- **OLD** — regras de proibição+exemplo (pink-elephant), verbatim do commit `b718ba5`.
- **NEW** — regras afirmativas da FASE 3a (`PROSE TEXTURE`).

Constantes entre braços: cenário (One Piece clone, `scenario_clone_op/…pt-br.json`),
14 story cards, mesma sequência de **18 inputs** de jogador (mini-aventura desenhada para
eliciar os 4 tics), provider **DeepSeek V4 flash** (o mesmo do baseline FASE 0), continuidade
de histórico dentro do braço. Swap OLD↔NEW por âncoras de fronteira no template — só o miolo
anti-tic muda; regras de coerência intactas.

Medição em duas camadas:
1. **Regex** (`tic_harness.py`) — âncora determinística para os tics sintáticos.
2. **LLM-judge cego** (workflow `fase3a-blind-judge`, 36 juízes + verificação adversarial de
   cada recap). Respostas anonimizadas e intercaladas (`prep_judge.py`); o juiz nunca vê o
   braço. Cada `npc_recap` alegado passa por um cético que tenta refutá-lo (refutou 1 de 4).

## Resultado (18 respostas/braço)

**Regex (determinístico):**

| tic | OLD /resp | NEW /resp | Δ |
|---|---|---|---|
| rule_of_three | 1.00 | 0.22 | **−78%** |
| fake_metrics | 0.11 | 0.00 | **−100%** |
| em_dash interruptivo | 0.17 | 0.00 | **−100%** |

**LLM-judge cego (semântico):**

| tic | OLD /resp | NEW /resp | Δ |
|---|---|---|---|
| npc_recaps (confirmado) | 0.11 | 0.06 | **−50%** |
| npc_recaps (bruto) | 0.17 | 0.06 | −67% |
| rule_of_three | 1.89 | 1.28 | **−32%** |
| fake_metrics | 0.44 | 0.22 | **−50%** |
| em_dash interruptivo | 0.11 | 0.00 | **−100%** |

**Todos os tics caem no braço NEW, nos dois métodos.** O efeito é mais forte e limpo nos
tics sintáticos. O juiz conta mais `rule_of_three` que o regex (pega trios adjetivais/
semânticos que o regex não vê), mas o delta é negativo em ambos.

Referência (baseline FASE 0, sessão real, 61 resp): rule_of_three 0.36, npc_recaps 0.26,
fake_metrics 0.31, em_dash ~4.6. Os valores absolutos do harness diferem do baseline (outro
tamanho/contexto de sessão) — por isso o **delta controlado** OLD→NEW é o número científico,
não a comparação cross-baseline.

## Nuances / limitações honestas

- **N=18/braço** é modesto (baseline: 61). Sinal claro nos tics sintáticos; `npc_recaps` com
  amostra pequena.
- **`npc_recaps` é ruidoso aqui:** o input idx14 (jogador dá um ultimato de 3 partes) força o
  NPC a repetir o ultimato nos **dois** braços — é o input, não a regra. O verify inclusive
  confirmou o recap no braço NEW nesse par (r28) e refutou no OLD (r29), invertendo o sinal
  nesse item isolado. O padrão de recap estrutural do baseline (NPC ecoa o jogador
  espontaneamente) quase não apareceu em nenhum braço com o prompt agnóstico — coerente com a
  hipótese de que a regra "ECO DE FALA" dos `tone_instructions` do seed era o amplificador
  real (fora do escopo da 3a; é conteúdo do usuário).
- **temperature 0.85** (default) → há variância; o pareamento por input a reduz.

## Conclusão

Hipótese da FASE 3a **confirmada**: remover os anti-exemplos pink-elephant e afirmar o
comportamento desejado reduz os tics medidos, sem regressão. Consistente com a medição do
OP-RPG (remover um anti-exemplo derrubou fabricação 50%→11%).

## Reproduzir

```
cd backend
PYTHONPATH=. python scripts/tic_harness.py          # gera docs/fase3a_ab/{new,old}.jsonl + regex
python scripts/prep_judge.py                          # anonimiza p/ juiz cego
# workflow fase3a-blind-judge  -> docs/fase3a_ab/judged_rows.json
python scripts/aggregate_ab.py docs/fase3a_ab/judged_rows.json
```
