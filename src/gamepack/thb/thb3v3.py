# -*- coding: utf-8 -*-
import random

from game.autoenv import Game, EventHandler, GameEnded, InterruptActionFlow, user_input, InputTransaction

from .actions import PlayerDeath, DrawCards, PlayerTurn, RevealIdentity
from .actions import action_eventhandlers

from .characters.baseclasses import mixin_character

from itertools import cycle
from collections import defaultdict

from utils import BatchList, Enum

from .common import PlayerIdentity, get_seed_for, sync_primitive, CharChoice
from .inputlets import ChooseGirlInputlet

import logging
log = logging.getLogger('THBattle')

_game_ehs = {}
_game_actions = {}


def game_eh(cls):
    _game_ehs[cls.__name__] = cls
    return cls


def game_action(cls):
    _game_actions[cls.__name__] = cls
    return cls


@game_eh
class DeathHandler(EventHandler):
    def handle(self, evt_type, act):
        if evt_type != 'action_after': return act
        if not isinstance(act, PlayerDeath): return act

        g = Game.getgame()

        # see if game ended
        force1, force2 = g.forces
        if all(p.dead or p.dropped for p in force1):
            g.winners = force2[:]
            raise GameEnded

        if all(p.dead or p.dropped for p in force2):
            g.winners = force1[:]
            raise GameEnded

        return act


class Identity(PlayerIdentity):
    class TYPE(Enum):
        HIDDEN = 0
        HAKUREI = 1
        MORIYA = 2


class THBattle(Game):
    n_persons = 6
    game_ehs = _game_ehs
    game_actions = _game_actions
    order_list = (0, 5, 3, 4, 2, 1)

    def game_start(g):
        # game started, init state
        from cards import Deck

        g.deck = Deck()

        g.ehclasses = ehclasses = list(action_eventhandlers) + g.game_ehs.values()

        for i, p in enumerate(g.players):
            p.identity = Identity()
            p.identity.type = (Identity.TYPE.HAKUREI, Identity.TYPE.MORIYA)[i % 2]

        g.forces = forces = BatchList([BatchList(), BatchList()])
        for i, p in enumerate(g.players):
            f = i % 2
            p.force = f
            forces[f].append(p)

        # choose girls -->
        from . import characters
        chars = list(characters.characters)

        seed = get_seed_for(g.players)
        random.Random(seed).shuffle(chars)

        # ANCHOR(test)
        chars.extend([
            # characters.kokoro.Kokoro,
        ])

        choices = [CharChoice(cls) for cls in chars[-16:]]
        del chars[-16:]

        for c in choices[:4]:
            c.char_cls = characters.akari.Akari

        if Game.SERVER_SIDE:
            for c, cls in zip(choices[:4], g.random.sample(chars, 4)):  # yes, must random.sample
                c.real_cls = cls

        # ----- roll ------
        roll = range(len(g.players))
        g.random.shuffle(roll)
        pl = g.players
        roll = sync_primitive(roll, pl)
        roll = [pl[i] for i in roll]
        g.emit_event('game_roll', roll)
        first = roll[0]
        g.emit_event('game_roll_result', first)
        # ----

        first_index = g.players.index(first)
        n = len(g.order_list)
        order = [g.players[(first_index + i) % n] for i in g.order_list]

        # akaris = {}  # DO NOT USE DICT! THEY ARE UNORDERED!
        akaris = []
        mapping = {p: choices for p in g.players}
        with InputTransaction('ChooseGirl', g.players, mapping=mapping) as trans:
            for p in order:
                c = user_input([p], ChooseGirlInputlet(g, mapping), timeout=30, trans=trans)
                c = c or [_c for _c in choices if not _c.chosen][0]
                c.chosen = p

                if issubclass(c.char_cls, characters.akari.Akari):
                    akaris.append((p, c))
                else:
                    g.set_character(p, c.char_cls)

                trans.notify('girl_chosen', c)

        # reveal akaris
        if akaris:
            for p, c in akaris:
                c.char_cls = c.real_cls

            g.players.reveal([i[1] for i in akaris])

            for p, c in akaris:
                g.set_character(p, c.char_cls)

        g.event_handlers = EventHandler.make_list(ehclasses)

        # -------
        for p in g.players:
            log.info(u'>> Player: %s:%s %s', p.__class__.__name__, Identity.TYPE.rlookup(p.identity.type), p.account.username)
        # -------

        first = g.players[first_index]

        try:
            pl = g.players
            for p in pl:
                g.process_action(RevealIdentity(p, pl))

            g.emit_event('game_begin', g)

            for p in g.players:
                g.process_action(DrawCards(p, amount=3 if p is first else 4))

            pl = g.players.rotate_to(first)

            for i, p in enumerate(cycle(pl)):
                if i >= 6000: break
                if not p.dead:
                    g.emit_event('player_turn', p)
                    try:
                        g.process_action(PlayerTurn(p))
                    except InterruptActionFlow:
                        pass

        except GameEnded:
            pass

        log.info(u'>> Winner: %s', Identity.TYPE.rlookup(g.winners[0].identity.type))

    def can_leave(self, p):
        return getattr(p, 'dead', False)

    def set_character(g, p, cls):
        # mix char class with player -->
        new, old_cls = mixin_character(p, cls)
        g.decorate(new)
        g.players.replace(p, new)
        g.forces[0].replace(p, new)
        g.forces[1].replace(p, new)
        assert not old_cls
        ehs = g.ehclasses
        ehs.extend(cls.eventhandlers_required)
        g.emit_event('switch_character', new)
        return new

    def decorate(g, p):
        from .cards import CardList
        from .characters.baseclasses import Character
        assert isinstance(p, Character)

        p.cards = CardList(p, 'cards')  # Cards in hand
        p.showncards = CardList(p, 'showncards')  # Cards which are shown to the others, treated as 'Cards in hand'
        p.equips = CardList(p, 'equips')  # Equipments
        p.fatetell = CardList(p, 'fatetell')  # Cards in the Fatetell Zone
        p.special = CardList(p, 'special')  # used on special purpose
        p.showncardlists = [p.showncards, p.fatetell]
        p.tags = defaultdict(int)
