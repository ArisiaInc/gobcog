# -*- coding: utf-8 -*-
import asyncio
import logging
import time
from operator import itemgetter

import discord
from beautifultable import ALIGN_LEFT, BeautifulTable
from redbot.core import commands
from redbot.core.i18n import Translator
from redbot.core.utils import AsyncIter
from redbot.core.utils.chat_formatting import box, humanize_list, humanize_number
from redbot.core.utils.menus import start_adding_reactions
from redbot.core.utils.predicates import ReactionPredicate

from .abc import AdventureMixin
from .bank import bank
from .charsheet import Character, Item
from .constants import ORDER
from .converters import EquipableItemConverter, EquipmentConverter
from .helpers import _title_case, escape, smart_embed
from .menus import BaseMenu, SimpleSource

_ = Translator("Adventure", __file__)

log = logging.getLogger("red.cogs.adventure")


class CharacterCommands(AdventureMixin):
    """This class handles character sheet adjustments by the player"""

    @commands.command()
    @commands.cooldown(rate=1, per=2, type=commands.BucketType.user)
    async def skill(self, ctx: commands.Context, spend: str = None, amount: int = 1):
        """This allows you to spend skillpoints.

        `[p]skill attack/charisma/intelligence`
        `[p]skill reset` Will allow you to reset your skill points for a cost.
        """
        if self.in_adventure(ctx):
            return await smart_embed(
                ctx,
                _("The skill cleric is back in town and the monster ahead of you is demanding your attention."),
            )
        if not await self.allow_in_dm(ctx):
            return await smart_embed(ctx, _("This command is not available in DM's on this bot."))
        if amount < 1:
            return await smart_embed(ctx, _("Nice try :smirk:"))
        async with self.get_lock(ctx.author):
            try:
                c = await Character.from_json(ctx, self.config, ctx.author, self._daily_bonus)
            except Exception as exc:
                log.exception("Error with the new character sheet", exc_info=exc)
                return
            if spend == "reset":
                last_reset = await self.config.user(ctx.author).last_skill_reset()
                if last_reset + 3600 > time.time():
                    return await smart_embed(ctx, _("You reset your skills within the last hour, try again later."))
                bal = c.bal
                currency_name = await bank.get_currency_name(
                    ctx.guild,
                )
                offering = min(int(bal / 5 + (c.total_int // 3)), 1000000000)
                if not await bank.can_spend(ctx.author, offering):
                    return await smart_embed(
                        ctx,
                        _("{author.mention}, you don't have enough {name}.").format(
                            author=ctx.author, name=await bank.get_currency_name(ctx.guild)
                        ),
                    )
                nv_msg = await ctx.send(
                    _(
                        "{author}, this will cost you at least {offering} {currency_name}.\n"
                        "You currently have {bal}. Do you want to proceed?"
                    ).format(
                        author=escape(ctx.author.display_name),
                        offering=humanize_number(offering),
                        currency_name=currency_name,
                        bal=humanize_number(bal),
                    )
                )
                start_adding_reactions(nv_msg, ReactionPredicate.YES_OR_NO_EMOJIS)
                pred = ReactionPredicate.yes_or_no(nv_msg, ctx.author)
                try:
                    await ctx.bot.wait_for("reaction_add", check=pred, timeout=60)
                except asyncio.TimeoutError:
                    await self._clear_react(nv_msg)
                    return

                if pred.result:
                    c.skill["pool"] += c.skill["att"] + c.skill["cha"] + c.skill["int"]
                    c.skill["att"] = 0
                    c.skill["cha"] = 0
                    c.skill["int"] = 0
                    await self.config.user(ctx.author).set(await c.to_json(ctx, self.config))
                    await self.config.user(ctx.author).last_skill_reset.set(int(time.time()))
                    await bank.withdraw_credits(ctx.author, offering)
                    await smart_embed(
                        ctx,
                        _("{}, your skill points have been reset.").format(escape(ctx.author.display_name)),
                    )
                else:
                    await smart_embed(
                        ctx,
                        _("Don't play games with me, {}.").format(escape(ctx.author.display_name)),
                    )
                return

            if c.skill["pool"] <= 0:
                return await smart_embed(
                    ctx,
                    _("{}, you do not have unspent skillpoints.").format(escape(ctx.author.display_name)),
                )
            elif c.skill["pool"] < amount:
                return await smart_embed(
                    ctx,
                    _("{}, you only have {} unspent skillpoints.").format(
                        escape(ctx.author.display_name), c.skill["pool"]
                    ),
                )
            if spend is None:
                await smart_embed(
                    ctx,
                    _(
                        "**{author}**, you currently have **{skillpoints}** unspent skillpoints.\n"
                        "If you want to put them towards a permanent attack, "
                        "charisma or intelligence bonus, use "
                        "`{prefix}skill attack`, `{prefix}skill charisma` or "
                        "`{prefix}skill intelligence`"
                    ).format(
                        author=escape(ctx.author.display_name),
                        skillpoints=str(c.skill["pool"]),
                        prefix=ctx.prefix,
                    ),
                )
            else:
                att = ["attack", "att", "atk"]
                cha = ["diplomacy", "charisma", "cha", "dipl"]
                intel = ["intelligence", "intellect", "int", "magic"]
                if spend not in att + cha + intel:
                    return await smart_embed(
                        ctx, _("Don't try to fool me! There is no such thing as {}.").format(spend)
                    )
                elif spend in att:
                    c.skill["pool"] -= amount
                    c.skill["att"] += amount
                    spend = "attack"
                elif spend in cha:
                    c.skill["pool"] -= amount
                    c.skill["cha"] += amount
                    spend = "charisma"
                elif spend in intel:
                    c.skill["pool"] -= amount
                    c.skill["int"] += amount
                    spend = "intelligence"
                await self.config.user(ctx.author).set(await c.to_json(ctx, self.config))
                await smart_embed(
                    ctx,
                    _("{author}, you permanently raised your {spend} value by {amount}.").format(
                        author=escape(ctx.author.display_name), spend=spend, amount=amount
                    ),
                )

    @commands.command(name="setinfo")
    @commands.bot_has_permissions(add_reactions=True, embed_links=True)
    async def set_show(self, ctx: commands.Context, *, set_name: str = None):
        """Show set bonuses for the specified set."""

        set_list = humanize_list(sorted([f"`{i}`" for i in self.SET_BONUSES.keys()], key=str.lower))
        if set_name is None:
            return await smart_embed(
                ctx,
                _("Use this command with one of the following set names: \n{sets}").format(sets=set_list),
            )

        title_cased_set_name = await _title_case(set_name)
        sets = self.SET_BONUSES.get(title_cased_set_name)
        if sets is None:
            return await smart_embed(
                ctx,
                _("`{input}` is not a valid set.\n\nPlease use one of the following full set names: \n{sets}").format(
                    input=title_cased_set_name, sets=set_list
                ),
            )

        try:
            c = await Character.from_json(ctx, self.config, ctx.author, self._daily_bonus)
        except Exception as exc:
            log.exception("Error with the new character sheet", exc_info=exc)
            return

        bonus_list = sorted(sets, key=itemgetter("parts"))
        msg_list = []
        for bonus in bonus_list:
            parts = bonus.get("parts", 0)
            attack = bonus.get("att", 0)
            charisma = bonus.get("cha", 0)
            intelligence = bonus.get("int", 0)
            dexterity = bonus.get("dex", 0)
            luck = bonus.get("luck", 0)

            attack = f"+{attack}" if attack > 0 else f"{attack}"
            charisma = f"+{charisma}" if charisma > 0 else f"{charisma}"
            intelligence = f"+{intelligence}" if intelligence > 0 else f"{intelligence}"
            dexterity = f"+{dexterity}" if dexterity > 0 else f"{dexterity}"
            luck = f"+{luck}" if luck > 0 else f"{luck}"

            statmult = round((bonus.get("statmult", 1) - 1) * 100)
            xpmult = round((bonus.get("xpmult", 1) - 1) * 100)
            cpmult = round((bonus.get("cpmult", 1) - 1) * 100)

            statmult = f"+{statmult}%" if statmult > 0 else f"{statmult}%"
            xpmult = f"+{xpmult}%" if xpmult > 0 else f"{xpmult}%"
            cpmult = f"+{cpmult}%" if cpmult > 0 else f"{cpmult}%"

            breakdown = _(
                "Attack:                [{attack}]\n"
                "Charisma:              [{charisma}]\n"
                "Intelligence:          [{intelligence}]\n"
                "Dexterity:             [{dexterity}]\n"
                "Luck:                  [{luck}]\n"
                "Stat Mulitplier:       [{statmult}]\n"
                "XP Multiplier:         [{xpmult}]\n"
                "Currency Multiplier:   [{cpmult}]\n\n"
            ).format(
                attack=attack,
                charisma=charisma,
                intelligence=intelligence,
                dexterity=dexterity,
                luck=luck,
                statmult=statmult,
                xpmult=xpmult,
                cpmult=cpmult,
            )
            stats_msg = _("{set_name} - {part_val} Part Bonus\n\n").format(
                set_name=title_cased_set_name, part_val=parts
            )
            stats_msg += breakdown
            stats_msg += "Multiple complete set bonuses stack."
            msg_list.append(box(stats_msg, lang="ini"))
        set_items = {key: value for key, value in self.TR_GEAR_SET.items() if value["set"] == title_cased_set_name}

        d = {}
        for k, v in set_items.items():
            if len(v["slot"]) > 1:
                d.update({v["slot"][0]: {k: v}})
                d.update({v["slot"][1]: {k: v}})
            else:
                d.update({v["slot"][0]: {k: v}})

        loadout_display = await self._build_loadout_display(ctx, {"items": d}, loadout=False, rebirths=c.rebirths)
        set_msg = _("{set_name} Set Pieces\n\n").format(set_name=title_cased_set_name)
        set_msg += loadout_display
        msg_list.append(box(set_msg, lang="css"))
        backpack_contents = await c.get_backpack(set_name=title_cased_set_name, clean=True)
        if backpack_contents:
            msg_list.extend(backpack_contents)
        await BaseMenu(
            source=SimpleSource(msg_list),
            delete_message_after=True,
            clear_reactions_after=True,
            timeout=60,
        ).start(ctx=ctx)

    @commands.command()
    @commands.bot_has_permissions(add_reactions=True)
    async def stats(self, ctx: commands.Context, *, user: discord.Member = None):
        """This draws up a character sheet of you or an optionally specified member."""
        if not await self.allow_in_dm(ctx):
            return await smart_embed(ctx, _("This command is not available in DM's on this bot."))
        if user is None:
            user = ctx.author
        if user.bot:
            return
        try:
            c = await Character.from_json(ctx, self.config, user, self._daily_bonus)
        except Exception:
            log.exception("Error with the new character sheet")
            return
        items = c.get_current_equipment(return_place_holder=True)
        msg = _("{}'s Character Sheet\n\n").format(escape(user.display_name))
        msg_len = len(msg)
        items_names = set()
        table = BeautifulTable(default_alignment=ALIGN_LEFT, maxwidth=500)
        table.set_style(BeautifulTable.STYLE_RST)
        msgs = []
        total = len(items)
        table.columns.header = [
            "Name",
            "Slot",
            "ATT",
            "CHA",
            "INT",
            "DEX",
            "LUC",
            "LVL",
            "QTY",
            "DEG",
            "SET",
        ]
        async for index, item in AsyncIter(items, steps=100).enumerate(start=1):
            if len(str(table)) > 1500:
                msgs.append(box(msg + str(table) + f"\nPage {len(msgs) + 1}", lang="css"))
                table = BeautifulTable(default_alignment=ALIGN_LEFT, maxwidth=500)
                table.set_style(BeautifulTable.STYLE_RST)
                table.columns.header = [
                    "Name",
                    "Slot",
                    "ATT",
                    "CHA",
                    "INT",
                    "DEX",
                    "LUC",
                    "LVL",
                    "QTY",
                    "DEG",
                    "SET",
                ]
            item_name = str(item)
            slots = len(item.slot)
            slot_name = item.slot[0] if slots == 1 else "two handed"
            if (item_name, slots, slot_name) in items_names:
                continue
            items_names.add((item_name, slots, slot_name))
            data = (
                item_name,
                slot_name,
                item.att * (1 if slots == 1 else 2),
                item.cha * (1 if slots == 1 else 2),
                item.int * (1 if slots == 1 else 2),
                item.dex * (1 if slots == 1 else 2),
                item.luck * (1 if slots == 1 else 2),
                f"[{r}]" if (r := c.equip_level(item)) is not None and r > c.lvl else f"{r}",
                item.owned,
                f"[{item.degrade}]"
                if item.rarity in ["legendary", "event", "ascended"] and item.degrade >= 0
                else "N/A",
                item.set or "N/A",
            )
            if data not in table.rows:
                table.rows.append(data)
            if index == total:
                table.set_style(BeautifulTable.STYLE_RST)
                msgs.append(box(msg + str(table) + f"\nPage {len(msgs) + 1}", lang="css"))
        await BaseMenu(
            source=SimpleSource([box(c, lang="css"), *msgs]),
            delete_message_after=True,
            clear_reactions_after=True,
            timeout=60,
        ).start(ctx=ctx)

    async def _build_loadout_display(
        self, ctx: commands.Context, userdata, loadout=True, rebirths: int = None, index: int = None
    ):
        table = BeautifulTable(default_alignment=ALIGN_LEFT, maxwidth=500)
        table.set_style(BeautifulTable.STYLE_RST)
        table.columns.header = [
            "Name",
            "Slot",
            "ATT",
            "CHA",
            "INT",
            "DEX",
            "LUC",
            "LVL",
            "SET",
        ]
        form_string = ""
        last_slot = ""
        att = 0
        cha = 0
        intel = 0
        dex = 0
        luck = 0

        def get_slot_index(slot):
            slot = slot[0]
            if slot not in ORDER:
                return float("inf")
            return ORDER.index(slot)

        data_sorted = sorted(userdata["items"].items(), key=get_slot_index)
        items_names = set()
        for (slot, data) in data_sorted:
            if slot == "backpack":
                continue
            if last_slot == "two handed":
                last_slot = slot
                continue
            if not data:
                continue
            item = Item.from_json(ctx, data)
            item_name = str(item)
            slots = len(item.slot)
            slot_name = item.slot[0] if slots == 1 else "two handed"
            if (item_name, slots, slot_name) in items_names:
                continue
            items_names.add((item_name, slots, slot_name))
            data = (
                item_name,
                slot_name,
                item.att * (1 if slots == 1 else 2),
                item.cha * (1 if slots == 1 else 2),
                item.int * (1 if slots == 1 else 2),
                item.dex * (1 if slots == 1 else 2),
                item.luck * (1 if slots == 1 else 2),
                item.lvl if item.rarity == "event" else max(item.lvl - min(max(rebirths // 2 - 1, 0), 50), 1),
                item.set or "N/A",
            )
            if data not in table.rows:
                table.rows.append(data)
            att += item.att
            cha += item.cha
            intel += item.int
            dex += item.dex
            luck += item.luck

        table.set_style(BeautifulTable.STYLE_RST)
        form_string += str(table)

        form_string += _("\n\nTotal stats: ")
        form_string += f"({att} | {cha} | {intel} | {dex} | {luck})"
        if index is not None:
            form_string += f"\nPage {index}"
        return form_string

    @commands.command()
    async def unequip(self, ctx: commands.Context, *, item: EquipmentConverter):
        """This stashes a specified equipped item into your backpack.

        Use `[p]unequip name of item` or `[p]unequip slot`
        """
        if self.in_adventure(ctx):
            return await smart_embed(
                ctx,
                _("You tried to unequip your items, but the monster ahead of you looks mighty hungry..."),
            )
        if not await self.allow_in_dm(ctx):
            return await smart_embed(ctx, _("This command is not available in DM's on this bot."))
        async with self.get_lock(ctx.author):
            try:
                c = await Character.from_json(ctx, self.config, ctx.author, self._daily_bonus)
            except Exception as exc:
                log.exception("Error with the new character sheet", exc_info=exc)
                return
            slots = [
                "head",
                "neck",
                "chest",
                "gloves",
                "belt",
                "legs",
                "boots",
                "left",
                "right",
                "ring",
                "charm",
            ]
            msg = ""
            if isinstance(item, list):
                for i in item:
                    await c.unequip_item(i)
                msg = _("{author} unequipped all their items and put them into their backpack.").format(
                    author=escape(ctx.author.display_name)
                )
            elif item in slots:
                current_item = getattr(c, item, None)
                if not current_item:
                    msg = _("{author}, you do not have an item equipped in the {item} slot.").format(
                        author=escape(ctx.author.display_name), item=item
                    )
                    return await ctx.send(box(msg, lang="css"))
                await c.unequip_item(current_item)
                msg = _("{author} removed the {current_item} and put it into their backpack.").format(
                    author=escape(ctx.author.display_name), current_item=current_item
                )
            else:
                for current_item in c.get_current_equipment():
                    if item.name.lower() in current_item.name.lower():
                        await c.unequip_item(current_item)
                        msg = _("{author} removed the {current_item} and put it into their backpack.").format(
                            author=escape(ctx.author.display_name), current_item=current_item
                        )
                        # We break if this works because unequip
                        # will autmatically remove multiple items
                        break
            if msg:
                await ctx.send(box(msg, lang="css"))
                await self.config.user(ctx.author).set(await c.to_json(ctx, self.config))
            else:
                await smart_embed(
                    ctx,
                    _("{author}, you do not have an item matching {item} equipped.").format(
                        author=escape(ctx.author.display_name), item=item
                    ),
                )

    @commands.command()
    async def equip(self, ctx: commands.Context, *, item: EquipableItemConverter):
        """This equips an item from your backpack."""
        if self.in_adventure(ctx):
            return await smart_embed(
                ctx,
                _("You tried to equip your item but the monster ahead nearly decapitated you."),
            )
        if not await self.allow_in_dm(ctx):
            return await smart_embed(ctx, _("This command is not available in DM's on this bot."))

        await ctx.invoke(self.backpack_equip, equip_item=item)
