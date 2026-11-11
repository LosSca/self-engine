"""FASE 3a A/B harness — generates narrator prose under OLD (pink-elephant) vs
NEW (affirmative) anti-tic rules, holding scenario + inputs fixed, so the rule
change is the only variable. Prose is saved to docs/fase3a_ab/{old,new}.jsonl
and a regex sanity-count of the syntactic tics is printed. NPC-recap (the
structural tic) is scored separately by an LLM judge downstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
logging.disable(logging.WARNING)

from app.config import settings

for _k, _v in [
    ("DEEPSEEK_API_KEY", settings.deepseek_api_key),
    ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
    ("OPENAI_API_KEY", settings.openai_api_key),
]:
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v

from app.engines.llm_router import LLMRouter, LLMConfig, LLMProvider
from app.engines.narrator_engine import NarratorEngine

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SCENARIO = os.path.join(_ROOT, "scenario_clone_op", "one_piece_adventures_lunar.pt-br.json")
_OUTDIR = os.path.join(_ROOT, "docs", "fase3a_ab")

# ── OLD anti-tic blocks (pink-elephant, verbatim as removed by FASE 3a) ──
OLD_EN = (
    "\nANTI-CLICHÉ / STYLISTIC VARIATION (CRITICAL — these tics break immersion):\n"
    "- NPCs do NOT recap the player's actions back to them. FORBIDDEN: NPC dialogue that lists what the player just did or said as an enumerated chain (e.g. \"You arrive on my island. You ask for the name. You say you have a map. And you want to know if I know him.\" / \"You appeared out of nowhere, freed a prisoner, demanded to see the prison, and now you're inviting me to be your nakama.\"). A real character REACTS to the action — they do not echo it back as a checklist to prove they were listening. To show comprehension, use indirection: a single pointed question, a held silence, a gesture, a re-framing in the NPC's own words and worldview.\n"
    "- AVOID the obsessive 'rule of three'. Short-clause triplets (\"One. Two. Three.\" / \"No boat. No crew. No plan.\" / \"There is no X. There is no Y. What there is, is Z.\") are an LLM tic — use AT MOST once every several responses, and only when genuinely earned by rhythm. Vary cadence: pairs, quartets, or a single long flowing sentence are usually stronger.\n"
    "- FORBIDDEN: pseudo-precise metric description. Phrases like \"two millimeters to the side\", \"half a second longer than necessary\", \"one millimeter\", \"three respirations\" — fake quantification in sensory observation reads as robotic. Use qualitative observation: \"a fraction\", \"almost imperceptibly\", \"just long enough to notice\", \"the briefest pause\".\n"
)
OLD_PT = (
    "\nANTI-VÍCIOS DE LINGUAGEM (CRÍTICO — estes tiques quebram imersão):\n"
    "- NPCs NÃO recapitulam as ações do jogador de volta pra ele. PROIBIDO: fala de NPC que lista o que o jogador acabou de fazer ou dizer numa cadeia enumerada (ex: \"Você chega na minha ilha. Pergunta pelo nome que nenhum forasteiro deveria saber. Diz que tem um mapa da operação. E quer saber se eu sei quem ele é.\" / \"Você apareceu do nada, soltou um prisioneiro na frente da Marinha inteira, exigiu ver a prisão, e agora está me convidando pra ser sua nakama.\"). Um personagem real REAGE à ação — ele NÃO ecoa a ação de volta como checklist pra provar que estava prestando atenção. Pra demonstrar que entendeu, use indireção: uma única pergunta direta, um silêncio sustentado, um gesto, ou uma releitura da situação nas palavras e na visão de mundo do próprio NPC.\n"
    "- EVITE a 'regra de três' obsessiva. Trios de frases curtas (\"Uma. Duas. Três.\" / \"Sem barco. Sem tripulação. Sem plano.\" / \"Não há X. Não há Y. O que há é Z.\") são um vício de LLM — use NO MÁXIMO uma vez a cada várias respostas, e somente quando o ritmo realmente pedir. Varie a cadência: pares, quartetos, ou uma única frase longa e fluida costumam ser mais fortes.\n"
    "- PROIBIDO pseudo-precisão métrica. Frases como \"dois milímetros pro lado\", \"meio segundo a mais do que o necessário\", \"um milímetro\", \"três respirações\" — quantificação falsa em observação sensorial soa robótica. Use observação qualitativa: \"uma fração\", \"quase imperceptivelmente\", \"o suficiente pra notar\", \"a menor das pausas\".\n"
)


def _swap(template: str, start_anchor: str, end_anchor: str, old_block: str) -> str:
    i = template.index(start_anchor)
    j = template.index(end_anchor)
    return template[:i] + old_block + template[j:]


def old_rules_dict() -> dict:
    cur = NarratorEngine._NARRATOR_RULES
    return {
        "en": _swap(cur["en"], "\nPROSE TEXTURE:", "\nCOHERENCE RULES", OLD_EN),
        "pt-br": _swap(cur["pt-br"], "\nTEXTURA DA PROSA:", "\nREGRAS DE COERÊNCIA", OLD_PT),
    }


NEW_RULES = dict(NarratorEngine._NARRATOR_RULES)
OLD_RULES = old_rules_dict()

PLAYER_INPUTS = [
    "[DO] Aporto a jangada na praia rochosa e me aproximo da fogueira onde vejo três homens grandes e um velho curvado sentado na areia.",
    "[SAY] Ei! O que vocês pensam que estão fazendo com esse velho?",
    "[DO] Pergunto ao mais velho o nome dele e o que está acontecendo nessa ilha.",
    "[SAY] Deixa eu adivinhar: vocês são bandidos, tomaram a vila, roubaram a comida e agora cobram pedágio de quem passa.",
    "[DO] Observo o líder dos bandidos com atenção, medindo cada gesto dele antes de decidir o que fazer.",
    "[DO] Avanço e desarmo o líder com um golpe seco, sem dar tempo dele reagir.",
    "[SAY] Mais alguém quer tentar? Ou vão me contar o que realmente está rolando aqui?",
    "[DO] Ajudo o velho a se levantar e pergunto quem manda de verdade nessa ilha.",
    "[SAY] Então tem um chefe pirata cobrando tributo, a vila está passando fome, e ninguém teve coragem de reagir. É isso mesmo?",
    "[DO] Sigo com o velho até a vila, reparando nas casas fechadas e nos rostos assustados pelo caminho.",
    "[SAY] Me leva até esse chefe. Quero olhar na cara dele.",
    "[DO] Espero em silêncio na entrada do pátio, estudando as saídas e deixando a tensão crescer antes de entrar.",
    "[SAY] Você deve ser o chefe. Bonito esse trono de caixotes.",
    "[DO] Encaro o chefe pirata e espero a reação dele.",
    "[SAY] Vim te dar uma escolha simples: devolve a comida, some da ilha, ou a gente resolve do jeito difícil.",
    "[DO] Dou um passo à frente e deixo a eletricidade formigar nas pontas dos dedos.",
    "[DO] Quando ele hesita, avanço e encerro a luta antes que ela comece de verdade.",
    "[DO] Volto pra praia, empurro a jangada de volta ao mar e sigo viagem, deitando pra olhar o céu.",
]


def load_scenario_context():
    d = json.load(open(_SCENARIO, encoding="utf-8"))
    tone = d["scenario"].get("tone_instructions", "")
    cards = d.get("story_cards", [])
    keys = ("east blue", "vila", "foosha", "dawn", "bandido", "pirata", "taverna",
            "marinha", "aldeia", "makino", "reino")
    picked, seen = [], set()
    for c in cards:
        if (c.get("card_type") or "").upper() not in ("NPC", "LOCATION", "FACTION"):
            continue
        name = (c.get("name") or "").strip()
        blob = (name + " " + str(c.get("content", {}).get("trigger_keys", ""))).lower()
        if any(k in blob for k in keys) and name not in seen:
            picked.append(c)
            seen.add(name)
        if len(picked) >= 14:
            break
    lines = ["STORY CARDS (world reference):"]
    for c in picked:
        desc = (c.get("content", {}).get("description") or "").strip().replace("\n", " ")
        lines.append(f"- [{c.get('card_type')}] {c.get('name')}: {desc}")
    return tone, "\n".join(lines)


async def run_arm(arm: str, rules: dict, tone: str, cards_ctx: str):
    cfg = LLMConfig()  # DeepSeek V4 flash — same provider as FASE 0 baseline
    router = LLMRouter(cfg)
    narr = NarratorEngine(router)
    orig = NarratorEngine._NARRATOR_RULES
    NarratorEngine._NARRATOR_RULES = rules
    results, history = [], []
    try:
        for idx, pin in enumerate(PLAYER_INPUTS):
            sysp = narr.build_system_prompt(
                tone_instructions=tone,
                memory_context="",
                language="pt-br",
                story_cards_context=cards_ctx,
                max_tokens=1200,
            )
            out = ""
            async for ch in narr.stream_narrative(
                pin, sysp, list(history), context_window=cfg.get_context_window()
            ):
                out += ch
            out = out.strip()
            results.append({"arm": arm, "idx": idx, "input": pin, "text": out})
            history.append({"role": "user", "content": pin})
            history.append({"role": "assistant", "content": out})
            print(f"[{arm}] {idx+1}/{len(PLAYER_INPUTS)} len={len(out)}", flush=True)
    finally:
        NarratorEngine._NARRATOR_RULES = orig
    return results


# ── regex sanity counters (syntactic tics; recap is judged by LLM) ──
_METRIC_RE = re.compile(
    r"\b\d+([.,]\d+)?\s*(mil[ií]metros?|cent[ií]metros?|mm|cm|graus?|segundos?|"
    r"d[eé]cimos?\s+de\s+segundo|batidas?\s+de\s+cora[cç][aã]o|respira[cç][oõ]es?)\b",
    re.IGNORECASE,
)
_HALF_SEC_RE = re.compile(r"\bmeio\s+segundo\b|\bfra[cç][aã]o\s+de\s+segundo\b", re.IGNORECASE)
_EXACT_RE = re.compile(r"\bexat[oa]s?\b|\bexatamente\b", re.IGNORECASE)


def count_metrics(text: str) -> int:
    return len(_METRIC_RE.findall(text)) + len(_HALF_SEC_RE.findall(text)) + len(_EXACT_RE.findall(text))


def count_rule_of_three(text: str) -> int:
    # runs of 3 consecutive short clauses (<=5 words, no comma) ending in . ! ?
    frags = re.split(r"(?<=[.!?])\s+", text)
    short = [bool(f) and len(f.split()) <= 5 and "," not in f and re.search(r"[.!?]$", f)
             for f in frags]
    hits = 0
    i = 0
    while i < len(short) - 2:
        if short[i] and short[i + 1] and short[i + 2]:
            hits += 1
            i += 3
        else:
            i += 1
    # anaphora: 3+ short clauses starting with same word ("Sem X. Sem Y. Sem Z.")
    for m in re.finditer(r"(?:\b(\w+)\b[^.!?]{0,40}[.!?]\s+){3,}", text):
        seg = m.group(0)
        firsts = [s.split()[0].lower() for s in re.split(r"(?<=[.!?])\s+", seg) if s.split()]
        if len(firsts) >= 3 and len(set(firsts[:3])) == 1:
            hits += 1
    return hits


def count_em_dash_interruptive(text: str) -> int:
    total = text.count("—")
    openings = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("—"))
    return max(0, total - openings)


def summarize(results, arm):
    n = len(results)
    m = sum(count_metrics(r["text"]) for r in results)
    r3 = sum(count_rule_of_three(r["text"]) for r in results)
    ed = sum(count_em_dash_interruptive(r["text"]) for r in results)
    words = sum(len(r["text"].split()) for r in results)
    print(f"\n=== {arm} ({n} responses, {words} words) ===")
    print(f"  rule_of_three : {r3}  ({r3/n:.2f}/resp)")
    print(f"  fake_metrics  : {m}  ({m/n:.2f}/resp)")
    print(f"  em_dash_intr  : {ed}  ({ed/n:.2f}/resp)")
    return {"arm": arm, "n": n, "words": words, "rule_of_three": r3, "fake_metrics": m, "em_dash": ed}


async def main():
    os.makedirs(_OUTDIR, exist_ok=True)
    tone, cards_ctx = load_scenario_context()
    print(f"tone={len(tone)} chars, cards_ctx={len(cards_ctx)} chars, inputs={len(PLAYER_INPUTS)}", flush=True)
    new_res, old_res = await asyncio.gather(
        run_arm("NEW", NEW_RULES, tone, cards_ctx),
        run_arm("OLD", OLD_RULES, tone, cards_ctx),
    )
    for arm, res in (("new", new_res), ("old", old_res)):
        with open(os.path.join(_OUTDIR, f"{arm}.jsonl"), "w", encoding="utf-8") as f:
            for r in res:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    s_new = summarize(new_res, "NEW")
    s_old = summarize(old_res, "OLD")
    with open(os.path.join(_OUTDIR, "regex_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"new": s_new, "old": s_old}, f, ensure_ascii=False, indent=2)
    print("\nDONE — prose saved to docs/fase3a_ab/{new,old}.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
