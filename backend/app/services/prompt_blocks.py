"""Assembling the prompt fragments that do not depend on knowledge recall.

The narrator prompt is built from several independent blocks: the world's power
scale, the player's character sheet, story cards, NPC dossiers, memory. They all
answer "what should the model already know before it reads the player's
action?", and none of them may make a request fail.

They live in their own module because they share exactly that contract: each
block returns a string (possibly empty) and degrades to "" when its data is
missing. A block that raises is a bug in the block, not a reason to lose the
turn - and the caller's `_with_knowledge` wrapper is where that rule is stated.

The mixin reads attributes created by :class:`GameSession.__init__`
(`_story_cards`, `_player_character`, ...). It is a partition of one class, not
an independent component.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PromptBlocksMixin:
    """Render the data-derived blocks of the narrator prompt."""

    def _build_power_scale_reference(self) -> str:
        """Build a power scale reference from NPC story cards.

        Uses the existing NPC power_level values as calibration anchors.
        No hardcoded progression system — the scale comes entirely from the scenario data.
        """
        npc_powers = []
        for card in self._story_cards:
            if hasattr(card, 'card_type') and getattr(card.card_type, 'value', str(card.card_type)).upper() == "NPC":
                content = card.content if isinstance(card.content, dict) else {}
                power = content.get("power_level")
                if power is not None:
                    npc_powers.append((card.name, int(power)))
        if not npc_powers:
            return ""
        npc_powers.sort(key=lambda x: x[1], reverse=True)
        # Include both ends of the scale — top AND bottom anchors
        # so the LLM sees what "strong" and "weak" look like in this world.
        if len(npc_powers) <= 50:
            selected = npc_powers
        else:
            top = npc_powers[:25]
            bottom = npc_powers[-25:]
            # Deduplicate in case of overlap
            seen = {n for n, _ in top}
            for item in bottom:
                if item[0] not in seen:
                    top.append(item)
                    seen.add(item[0])
            selected = sorted(top, key=lambda x: x[1], reverse=True)
        lines = [f"  {name}: {p}/10" for name, p in selected]
        return (
            "WORLD POWER SCALE — use these characters as anchors when estimating power:\n"
            + "\n".join(lines)
            + "\n"
            "Unnamed creatures/enemies should be calibrated relative to these anchors."
        )

    def _build_character_setup_block(self) -> str:
        """Format the player identity into a CHARACTER SETUP block for the prompt.

        The block combines the setup answers and the character the player
        brought in from the library, and is injected into the narrator system
        prompt. Returns an empty string when neither exists."""
        lines: list[str] = []
        if self._setup_answers:
            lines.append("CHARACTER SETUP (locked from session start):")
        for answer in self._setup_answers.values():
            if not isinstance(answer, dict):
                continue
            var_name = answer.get("var_name") or ""
            value = answer.get("value") or ""
            description = answer.get("description") or ""
            if not var_name or not value:
                continue
            lines.append(f"- {var_name}: {value}")
            if description:
                lines.append(f"  {description}")
        player_lines = self._player_character_lines()
        if player_lines:
            if not lines:
                lines.append("CHARACTER SETUP (locked from session start):")
            lines.extend(player_lines)
        if len(lines) <= 1:  # header only, nothing to say
            return ""
        return "\n".join(lines)

    def _player_character_lines(self) -> "list[str]":
        """The library character bound to this campaign, as prompt lines.

        The character is authored outside the world, so the narrator has to be
        told who the protagonist is explicitly; otherwise it invents a name.
        Returns an empty list when no character was picked.
        """
        character = self._player_character or {}
        name = str(character.get("name") or "").strip()
        if not name:
            return []
        lines = [f"- player character: {name}"]
        role = str(character.get("role") or "").strip()
        if role:
            lines.append(f"  role: {role}")
        description = str(character.get("description") or "").strip()
        if description:
            lines.append(f"  {description}")
        return lines
